"""CalendarPort over CalDAV, for iCloud and any standard server.

Contract: docs/contracts/08-ports.md (P-1 to P-18) and 04-writes.md. Writes are one HTTP
request made conditional: `If-None-Match: *` for a create (so a repeat cannot duplicate)
and `If-Match: <etag>` for update and delete (so a changed event is never overwritten).
A 4xx answer or an unopened connection means nothing happened; a timeout, 5xx, 408 or 429
means the write may have happened and is settled only by reconcile.
"""

import base64
import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

from pydantic import JsonValue

from chamberlain.connectors.caldav import dav
from chamberlain.connectors.caldav.config import CalDavBinding, CalDavConfig
from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.calendar_support.fields import same_content
from chamberlain.connectors.calendar_support.http import (
    HttpReply,
    HttpTransport,
    TransportFailure,
    UrllibTransport,
    require_secure,
    write_may_have_landed,
)
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery, EventFields, EventStatus
from chamberlain.domain.capabilities import (
    Capability,
    CapabilityName,
    CapabilitySet,
    HealthState,
    SourceHealth,
    VerificationLevel,
)
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarIntent,
    CalendarRespond,
    CalendarUpdate,
    IntentKind,
)
from chamberlain.domain.operations import (
    DispatchOutcome,
    DispatchResult,
    Prepared,
    ProviderReceipt,
    ReconcileResult,
    ReconcileStatus,
    Verification,
)
from chamberlain.ports.clock import Clock
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_VERIFIED = Verification(client_verified=True, server_verified=True)
_PROVABLE = frozenset(
    {
        IntentKind.CALENDAR_CREATE.value,
        IntentKind.CALENDAR_UPDATE.value,
        IntentKind.CALENDAR_DELETE.value,
    }
)
_CALENDAR_TYPE = {"Content-Type": 'text/calendar; charset="utf-8"'}
_XML_TYPE = {"Content-Type": "application/xml; charset=utf-8"}

PasswordSource = Callable[[CalDavBinding], str]


def resource_name(uid: str) -> str:
    """The file a create writes: derived from the uid, so a repeat hits the same resource."""
    return hashlib.sha256(uid.encode()).hexdigest()[:40] + ".ics"


class CalDavCalendar:
    """Implements `chamberlain.ports.CalendarPort` for CalDAV calendar collections."""

    def __init__(
        self,
        config: CalDavConfig,
        passwords: PasswordSource,
        clock: Clock,
        transport: HttpTransport | None = None,
    ) -> None:
        for binding in config.bindings.values():
            require_secure(binding.url)
        self._config = config
        self._passwords = passwords
        self._clock = clock
        self._transport = transport or UrllibTransport()

    # --- description ----------------------------------------------------------

    def capabilities(self, source: str) -> CapabilitySet:
        self._binding(source)
        return CapabilitySet(
            source=source,
            capabilities=(
                Capability(
                    name=CapabilityName.CALENDAR_READ,
                    level=VerificationLevel.IMPLEMENTED,
                    limits={"page_size": 250},
                ),
                Capability(
                    name=CapabilityName.CALENDAR_WRITE,
                    level=VerificationLevel.IMPLEMENTED,
                    absence_provable=_PROVABLE,
                ),
            ),
        )

    def health(self, source: str) -> SourceHealth:
        binding = self._binding(source)
        now = self._clock.now()
        try:
            reply = self._send(
                binding, "PROPFIND", self._collection(binding), dav.PROPFIND_HEALTH, {"Depth": "0"}
            )
        except ChamberlainError as error:
            state = (
                HealthState.SIGN_IN_NEEDED
                if error.code is ErrorCode.SOURCE_SIGN_IN_NEEDED
                else HealthState.UNAVAILABLE
            )
            return SourceHealth(source=source, state=state, checked_at=now)
        except TransportFailure:
            return SourceHealth(source=source, state=HealthState.UNAVAILABLE, checked_at=now)
        if reply.status == 207:
            return SourceHealth(
                source=source, state=HealthState.OK, checked_at=now, fresh_as_of=now
            )
        sign_in = reply.status in (401, 403)
        state = HealthState.SIGN_IN_NEEDED if sign_in else HealthState.UNAVAILABLE
        return SourceHealth(source=source, state=state, checked_at=now)

    # --- reading --------------------------------------------------------------

    def events(
        self, source: str, query: CalendarQuery, cursor: str | None = None
    ) -> Page[CalendarEvent]:
        binding = self._binding(source)
        offset = _decode_cursor(cursor)
        found: list[CalendarEvent] = []
        gaps: list[Gap] = []
        body = dav.query_window(query.start, query.end, expand=True)
        for name, etag, text in self._report(binding, self._collection(binding), body):
            for parsed in ics.parse_events(text):
                if parsed.recurring and parsed.recurrence_id is None:
                    gaps.append(
                        Gap(source=source, reason="The server did not expand a recurring event.")
                    )
                if parsed.status is EventStatus.CANCELLED and not query.include_cancelled:
                    continue
                fields = parsed.fields
                if fields.end < query.start or fields.start > query.end:
                    continue
                if (
                    query.text
                    and query.text.casefold()
                    not in f"{fields.title}\n{fields.description}".casefold()
                ):
                    continue
                found.append(
                    _event(source, ics.instance_locator(name, parsed.recurrence_id), etag, parsed)
                )
        found.sort(key=lambda e: (e.fields.start, e.locator))
        window = found[offset : offset + query.limit]
        more = offset + query.limit < len(found)
        return Page[CalendarEvent](
            items=tuple(window),
            cursor=f"c1.{offset + query.limit}" if more else None,
            complete=not more and not gaps,
            gaps=tuple(gaps[:1]),
        )

    def read(self, source: str, locator: str) -> CalendarEvent:
        binding = self._binding(source)
        name, recurrence = ics.split_locator(locator)
        fetched = self._fetch(binding, name)
        if fetched is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        text, etag = fetched
        if recurrence is None:
            return _event(source, name, etag, ics.master_of(text))
        for parsed in ics.parse_events(text):
            if parsed.recurrence_id == recurrence:
                return _event(source, locator, etag, parsed)
        moment = datetime.strptime(recurrence, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        body = dav.query_window(moment, moment + timedelta(seconds=1), expand=True)
        for other, _, expanded in self._report(binding, self._collection(binding), body):
            if other != name:
                continue
            for parsed in ics.parse_events(expanded):
                if parsed.recurrence_id == recurrence:
                    return _event(source, locator, etag, parsed)
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That occurrence is not there.")

    # --- writing --------------------------------------------------------------

    def prepare(self, intent: CalendarIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only (P-9). Builds the exact resource text `dispatch` will send."""
        binding = self._binding(intent.source)
        now = self._clock.now()
        data: dict[str, JsonValue]
        token: str | None = None
        match intent:
            case CalendarCreate():
                if intent.uid is None:
                    raise ChamberlainError(
                        ErrorCode.REQUEST_INVALID, "The event has no uid.", field="uid"
                    )
                ics.refuse_invitations(None, intent.event)
                token = intent.uid
                data = {
                    "name": resource_name(intent.uid),
                    "ics": ics.build_new(intent.uid, intent.event, now),
                    "fields": intent.event.model_dump(mode="json"),
                }
            case CalendarUpdate():
                name, text, _ = self._current(
                    binding, refs, intent.event_ref, intent.expected_etag, intent.event
                )
                data = {
                    "name": name,
                    "expected_etag": intent.expected_etag,
                    "ics": ics.apply_fields(text, intent.event, now),
                    "fields": intent.event.model_dump(mode="json"),
                }
            case CalendarDelete():
                name, _, _ = self._current(
                    binding, refs, intent.event_ref, intent.expected_etag, None
                )
                data = {"name": name, "expected_etag": intent.expected_etag}
            case CalendarRespond():
                raise ics.no_invitations()
        return Prepared.model_validate(
            {
                "source": intent.source,
                "intent_kind": intent.kind,
                "reconcile_token": token,
                "data": data,
            }
        )

    def dispatch(self, prepared: Prepared, key: str) -> DispatchResult:
        """The one mutation (P-10). Never retries and never raises."""
        try:
            return self._dispatch(self._binding(prepared.source), prepared)
        except ChamberlainError as error:
            # Raised before anything was sent (sign-in, configuration): nothing happened.
            return DispatchResult(
                outcome=DispatchOutcome.NOT_APPLIED, detail=f"{error.code}: {error.detail}"
            )

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11). Settles only from a read-back."""
        try:
            return self._reconcile(self._binding(prepared.source), prepared)
        except (ChamberlainError, TransportFailure) as error:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail=_why(error))

    # --- dispatch -------------------------------------------------------------

    def _dispatch(self, binding: CalDavBinding, prepared: Prepared) -> DispatchResult:
        data = prepared.data
        kind = prepared.intent_kind
        name = _text(data, "name")
        url = self._resource(binding, name)
        headers: dict[str, str]
        if kind is IntentKind.CALENDAR_DELETE:
            method, body, headers = "DELETE", None, {"If-Match": _text(data, "expected_etag")}
        else:
            method, body = "PUT", _text(data, "ics").encode("utf-8")
            headers = dict(_CALENDAR_TYPE)
            if kind is IntentKind.CALENDAR_CREATE:
                headers["If-None-Match"] = "*"
            else:
                headers["If-Match"] = _text(data, "expected_etag")
        try:
            reply = self._send(binding, method, url, body, headers)
        except TransportFailure as error:
            outcome = DispatchOutcome.UNKNOWN if error.sent else DispatchOutcome.NOT_APPLIED
            return DispatchResult(outcome=outcome, detail=f"transport: {error}")
        if reply.status not in (200, 201, 204):
            outcome = (
                DispatchOutcome.UNKNOWN
                if write_may_have_landed(reply.status)
                else DispatchOutcome.NOT_APPLIED
            )
            return DispatchResult(outcome=outcome, detail=f"caldav http {reply.status}")
        return self._verify(binding, prepared, name)

    def _verify(self, binding: CalDavBinding, prepared: Prepared, name: str) -> DispatchResult:
        """The write was accepted. Read it back; an unreadable or different result is unknown."""
        kind = prepared.intent_kind
        try:
            fetched = self._fetch(binding, name)
            parsed = ics.master_of(fetched[0]) if fetched else None
        except (ChamberlainError, TransportFailure) as error:
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail=f"not verified: {_why(error)}"
            )
        if kind is IntentKind.CALENDAR_DELETE:
            if fetched is None:
                return DispatchResult(
                    outcome=DispatchOutcome.APPLIED,
                    receipt=ProviderReceipt(locator=name, verification=_VERIFIED),
                )
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="still present")
        wanted = EventFields.model_validate(prepared.data["fields"])
        if fetched is None or parsed is None or not same_content(parsed.fields, wanted):
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail="read-back differs from the request"
            )
        return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=_receipt(name, fetched[1]))

    # --- reconcile ------------------------------------------------------------

    def _reconcile(self, binding: CalDavBinding, prepared: Prepared) -> ReconcileResult:
        kind = prepared.intent_kind
        data = prepared.data
        if kind is IntentKind.CALENDAR_CREATE:
            return self._reconcile_create(binding, prepared)
        name = _text(data, "name")
        fetched = self._fetch(binding, name)
        if kind is IntentKind.CALENDAR_DELETE:
            if fetched is None:
                return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=_receipt(name, None))
            if fetched[1] == _text(data, "expected_etag"):
                return ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="changed otherwise")
        if fetched is None:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="event not found")
        text, etag = fetched
        if etag == _text(data, "expected_etag"):
            return ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)
        wanted = EventFields.model_validate(data["fields"])
        if same_content(ics.master_of(text).fields, wanted):
            return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=_receipt(name, etag))
        return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="changed otherwise")

    def _reconcile_create(self, binding: CalDavBinding, prepared: Prepared) -> ReconcileResult:
        name = _text(prepared.data, "name")
        fetched = self._fetch(binding, name)
        if fetched is not None:
            return ReconcileResult(
                status=ReconcileStatus.PRESENT, receipt=_receipt(name, fetched[1])
            )
        uid = prepared.reconcile_token or ""
        for other, etag, text in self._report(
            binding, self._collection(binding), dav.query_uid(uid)
        ):
            if any(p.uid == uid for p in ics.parse_events(text)):
                return ReconcileResult(
                    status=ReconcileStatus.PRESENT, receipt=_receipt(other, etag)
                )
        return ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)

    # --- transport ------------------------------------------------------------

    def _binding(self, source: str) -> CalDavBinding:
        binding = self._config.bindings.get(source)
        if binding is None:
            raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, "That source is not set up.")
        return binding

    @staticmethod
    def _collection(binding: CalDavBinding) -> str:
        return binding.url if binding.url.endswith("/") else binding.url + "/"

    def _resource(self, binding: CalDavBinding, name: str) -> str:
        return self._collection(binding) + quote(name, safe="")

    def _send(
        self,
        binding: CalDavBinding,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpReply:
        secret = base64.b64encode(
            f"{binding.username}:{self._passwords(binding)}".encode()
        ).decode()
        sent = {"Authorization": f"Basic {secret}", **(headers or {})}
        if body is not None and "Content-Type" not in sent:
            sent.update(_XML_TYPE)
        return self._transport.send(
            method, url, headers=sent, body=body, timeout=self._config.deadline_seconds
        )

    def _fetch(self, binding: CalDavBinding, name: str) -> tuple[str, str] | None:
        """The resource text and etag, or None when the server has no such resource."""
        try:
            reply = self._send(binding, "GET", self._resource(binding, name))
        except TransportFailure as error:
            raise _unavailable(str(error)) from None
        if reply.status == 404:
            return None
        _check(reply, (200,))
        return reply.body.decode("utf-8", "replace"), reply.headers.get("etag", "")

    def _report(self, binding: CalDavBinding, url: str, body: bytes) -> list[tuple[str, str, str]]:
        """`(name, etag, calendar-data)` for every event resource the query matches."""
        try:
            reply = self._send(binding, "REPORT", url, body, {"Depth": "1"})
        except TransportFailure as error:
            raise _unavailable(str(error)) from None
        _check(reply, (207,))
        try:
            responses = dav.parse_multistatus(reply.body)
        except ValueError:
            raise _unavailable("unreadable multistatus") from None
        collection_path = urlsplit(self._collection(binding)).path
        found: list[tuple[str, str, str]] = []
        for response in responses:
            name = dav.name_in(collection_path, response.href)
            if name is not None and response.calendar_data and response.etag:
                found.append((name, response.etag, response.calendar_data))
        return found

    def _current(
        self,
        binding: CalDavBinding,
        refs: Mapping[str, str],
        ref: str,
        expected_etag: str,
        wanted: EventFields | None,
    ) -> tuple[str, str, ics.ParsedEvent]:
        locator = refs.get(ref)
        if locator is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not known.")
        name, recurrence = ics.split_locator(locator)
        if recurrence is not None:
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "A single occurrence of a recurring event cannot be written.",
            )
        fetched = self._fetch(binding, name)
        if fetched is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        text, etag = fetched
        if etag != expected_etag:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The event changed since it was read.")
        parsed = ics.master_of(text)
        ics.refuse_invitations(parsed, wanted)
        return name, text, parsed


def _check(reply: HttpReply, accepted: tuple[int, ...]) -> None:
    if reply.status in accepted:
        return
    if reply.status in (401, 403):
        raise ChamberlainError(
            ErrorCode.SOURCE_SIGN_IN_NEEDED,
            "The calendar server did not accept the saved sign-in.",
            detail=f"http {reply.status}",
        )
    raise _unavailable(f"http {reply.status}")


def _event(source: str, locator: str, etag: str, parsed: ics.ParsedEvent) -> CalendarEvent:
    return CalendarEvent(
        source=source,
        locator=locator,
        uid=parsed.uid,
        etag=etag,
        status=parsed.status,
        fields=parsed.fields,
        updated=parsed.updated,
    )


def _receipt(name: str, etag: str | None) -> ProviderReceipt:
    return ProviderReceipt(locator=name, etag=etag or None, verification=_VERIFIED)


def _text(data: Mapping[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "A prepared field is missing.")
    return value


def _unavailable(detail: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.SOURCE_UNAVAILABLE, "The calendar server did not answer.", detail=detail
    )


def _why(error: Exception) -> str:
    return error.detail if isinstance(error, ChamberlainError) else str(error)


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix, _, number = cursor.partition(".")
    if prefix != "c1" or not number.isdigit():
        raise ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid.")
    return int(number)
