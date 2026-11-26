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
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from urllib.parse import quote, urlsplit

from pydantic import JsonValue

from chamberlain.connectors.caldav import dav
from chamberlain.connectors.caldav.config import CalDavBinding, CalDavConfig
from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.calendar_support.fields import attendees_match, same_content
from chamberlain.connectors.calendar_support.http import (
    HttpReply,
    HttpTransport,
    TransportFailure,
    UrllibTransport,
    calendar_unavailable,
    require_secure,
)
from chamberlain.connectors.calendar_support.paging import (
    matches_window,
    offset_cursor,
    offset_page,
)
from chamberlain.connectors.calendar_support.port import (
    CalendarWritePort,
    Current,
    Observation,
    WriteFailure,
    WriteReply,
    expected_etag,
    fields,
    payload,
    prepared_text,
)
from chamberlain.connectors.calendar_support.rrule import same_rule
from chamberlain.domain.calendar import (
    CalendarEvent,
    CalendarQuery,
    EventFields,
    EventStatus,
    Participant,
    Response,
)
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
from chamberlain.domain.operations import Prepared, ProviderReceipt, Verification
from chamberlain.domain.recurrence import parse_occurrence_id
from chamberlain.ports.clock import Clock
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_VERIFIED = Verification(client_verified=True, server_verified=True)
_PROVABLE = frozenset(
    {
        IntentKind.CALENDAR_CREATE.value,
        IntentKind.CALENDAR_UPDATE.value,
        IntentKind.CALENDAR_DELETE.value,
        IntentKind.CALENDAR_RESPOND.value,
    }
)
_PARTSTAT = {"accepted": "ACCEPTED", "tentative": "TENTATIVE", "declined": "DECLINED"}
_RESPONSES = {
    "ACCEPTED": Response.ACCEPTED,
    "TENTATIVE": Response.TENTATIVE,
    "DECLINED": Response.DECLINED,
}
_CALENDAR_TYPE = {"Content-Type": 'text/calendar; charset="utf-8"'}
_XML_TYPE = {"Content-Type": "application/xml; charset=utf-8"}

PasswordSource = Callable[[CalDavBinding], str]


def resource_name(uid: str) -> str:
    """The file a create writes: derived from the uid, so a repeat hits the same resource."""
    return hashlib.sha256(uid.encode()).hexdigest()[:40] + ".ics"


@dataclass(frozen=True)
class _Resource:
    name: str
    text: str
    master: ics.ParsedEvent
    occurrence: str | None


class CalDavCalendar(CalendarWritePort):
    """The common calendar port over CalDAV calendar collections."""

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
        binding = self._binding(source)
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
                *self._invite_capability(binding),
                *(
                    Capability(
                        name=name,
                        level=VerificationLevel.IMPLEMENTED,
                        absence_provable=_PROVABLE,
                    )
                    for name in (
                        CapabilityName.CALENDAR_RECURRENCE,
                        CapabilityName.CALENDAR_OCCURRENCE,
                    )
                ),
            ),
        )

    @staticmethod
    def _invite_capability(binding: CalDavBinding) -> tuple[Capability, ...]:
        """Invitations need to know the address they are sent from (RFC 6638 ORGANIZER)."""
        if own_address(binding) is None:
            return ()
        return (
            Capability(name=CapabilityName.CALENDAR_INVITE, level=VerificationLevel.IMPLEMENTED),
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
        offset = offset_cursor(cursor, "c1")
        found: list[CalendarEvent] = []
        gaps: list[Gap] = []
        body = dav.query_window(query.start, query.end, expand=True)
        for name, etag, text in self._report(binding, self._collection(binding), body):
            for parsed in ics.parse_events(text):
                if parsed.recurring and parsed.recurrence_id is None:
                    gaps.append(
                        Gap(source=source, reason="The server did not expand a recurring event.")
                    )
                if not matches_window(query, parsed.fields, parsed.status):
                    continue
                found.append(
                    _event(
                        source,
                        ics.instance_locator(name, parsed.recurrence_id),
                        etag,
                        parsed,
                        own_address(binding),
                    )
                )
        return offset_page(found, gaps, offset, query.limit, "c1")

    def read(self, source: str, locator: str) -> CalendarEvent:
        binding = self._binding(source)
        name, recurrence = ics.split_locator(locator)
        fetched = self._fetch(binding, name)
        if fetched is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        text, etag = fetched
        if recurrence is None:
            return _event(source, name, etag, ics.master_of(text), own_address(binding))
        occurrence = self._occurrence(binding, name, text, recurrence)
        if occurrence is not None:
            return _event(source, locator, etag, occurrence, own_address(binding))
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That occurrence is not there.")

    # --- writing --------------------------------------------------------------

    def resolve(self, source: str, locator: str) -> Current:
        binding = self._binding(source)
        name, occurrence = ics.split_locator(locator)
        fetched = self._fetch(binding, name)
        if fetched is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        text, etag = fetched
        master = ics.master_of(text)
        if occurrence is not None and self._occurrence(binding, name, text, occurrence) is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE, "That occurrence is not there."
            )
        return Current(
            locator=locator,
            etag=etag,
            part="occurrence" if occurrence else "series" if master.recurring else None,
            raw=_Resource(name, text, master, occurrence),
        )

    def validate(self, intent: CalendarIntent, current: Current | None) -> None:
        binding = self._binding(intent.source)
        if isinstance(intent, CalendarCreate):
            if intent.event.attendees:
                _need_address(binding)
            return
        assert current is not None
        resource = cast(_Resource, current.raw)
        master = resource.master
        me = own_address(binding)
        if isinstance(intent, CalendarRespond):
            address = _need_address(binding)
            if resource.occurrence is not None:
                raise ChamberlainError(
                    ErrorCode.CAPABILITY_UNSUPPORTED,
                    "A single occurrence of a recurring event cannot be answered.",
                )
            if not _is_invitee_copy(master, address) or address not in dict(master.partstats):
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "That event is not an invitation to this calendar."
                )
        elif _is_invitee_copy(master, me):
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "Only the organiser can change that event.",
                detail="invitee-copy",
            )
        elif isinstance(intent, CalendarUpdate) and (
            intent.event.attendees or master.fields.attendees
        ):
            _need_address(binding)

    def encode(self, intent: CalendarIntent, current: Current | None) -> dict[str, JsonValue]:
        binding = self._binding(intent.source)
        now = self._clock.now()
        if isinstance(intent, CalendarCreate):
            assert intent.uid is not None
            organiser = own_address(binding) if intent.event.attendees else None
            return {
                "name": resource_name(intent.uid),
                "ics": ics.build_new(intent.uid, intent.event, now, organiser),
            }
        assert current is not None
        resource = cast(_Resource, current.raw)
        data: dict[str, JsonValue] = {"name": resource.name, "occurrence": resource.occurrence}
        if isinstance(intent, CalendarUpdate):
            organiser = (
                own_address(binding)
                if intent.event.attendees or resource.master.fields.attendees
                else None
            )
            data["ics"] = (
                ics.apply_occurrence(resource.text, resource.occurrence, intent.event, now)
                if resource.occurrence is not None
                else ics.apply_fields(resource.text, intent.event, now, organiser)
            )
        elif isinstance(intent, CalendarDelete):
            if resource.occurrence is not None:
                data["ics"] = ics.cancel_occurrence(resource.text, resource.occurrence, now)
        else:
            me = _need_address(binding)
            data.update(
                ics=ics.apply_answer(resource.text, me, _PARTSTAT[intent.response], now),
                response=intent.response,
                address=me,
            )
        return data

    def write(self, prepared: Prepared) -> WriteReply:
        try:
            data = payload(prepared)
            kind = prepared.intent_kind
            name = prepared_text(data, "name")
            headers: dict[str, str]
            if kind is IntentKind.CALENDAR_DELETE and "ics" not in data:
                method, body, headers = "DELETE", None, {"If-Match": expected_etag(prepared)}
            else:
                method, body = "PUT", prepared_text(data, "ics").encode("utf-8")
                headers = dict(_CALENDAR_TYPE)
                if kind is IntentKind.CALENDAR_CREATE:
                    headers["If-None-Match"] = "*"
                else:
                    headers["If-Match"] = expected_etag(prepared)
            binding = self._binding(prepared.source)
            target = self._resource(binding, name)
        except Exception as error:
            raise WriteFailure(type(error).__name__, sent=False) from error
        try:
            reply = self._send(binding, method, target, body, headers)
        except TransportFailure as error:
            raise WriteFailure(f"transport: {error}", sent=error.sent) from None
        except ChamberlainError as error:
            raise WriteFailure(f"{error.code}: {error.detail}", sent=False) from None
        if reply.status not in (200, 201, 204):
            raise WriteFailure(f"caldav http {reply.status}", sent=True, status=reply.status)
        return WriteReply()

    def observe(self, prepared: Prepared, reply: WriteReply | None) -> Observation:
        binding = self._binding(prepared.source)
        data = payload(prepared)
        name = prepared_text(data, "name")
        kind = prepared.intent_kind
        occurrence = data.get("occurrence")
        fetched = self._fetch(binding, name)
        if fetched is None:
            if kind is IntentKind.CALENDAR_CREATE and reply is None:
                return self._find_created(binding, prepared)
            whole_delete = kind is IntentKind.CALENDAR_DELETE and occurrence is None
            return Observation(
                present=False,
                delete_confirmed=whole_delete,
                receipt=_receipt(name, None) if whole_delete else None,
            )
        text, etag = fetched
        master = ics.master_of(text)
        receipt = _receipt(name, etag)
        if kind is IntentKind.CALENDAR_CREATE:
            return _created(master, prepared, binding, receipt)
        unchanged = etag == expected_etag(prepared)
        if isinstance(occurrence, str):
            actual = ics.occurrence_of(text, occurrence)
            deleted = (actual is None and occurrence in master.cancelled_dates) or (
                actual is not None and actual.status is EventStatus.CANCELLED
            )
            matches = False
            if kind is IntentKind.CALENDAR_UPDATE and actual is not None:
                # The occurrence update preserves the series' guests. Compare the complete
                # encoded override, including any private classification and those guests.
                wanted = ics.occurrence_of(prepared_text(data, "ics"), occurrence)
                matches = wanted is not None and _same(actual, wanted.fields, binding)
            return Observation(
                present=not deleted,
                matches=matches,
                delete_confirmed=deleted,
                unchanged=unchanged,
                absence_provable=True,
                receipt=receipt,
            )
        if kind is IntentKind.CALENDAR_RESPOND:
            answer = dict(master.partstats).get(prepared_text(data, "address"))
            matches = answer == _PARTSTAT[prepared_text(data, "response")]
        elif kind is IntentKind.CALENDAR_UPDATE:
            wanted = fields(prepared)
            if wanted.recurrence is None:
                # A series update that supplies no rule must keep the original rule.
                encoded = ics.master_of(prepared_text(data, "ics"))
                wanted = wanted.model_copy(update={"recurrence": encoded.rule})
            matches = _same(master, wanted, binding)
        else:
            matches = False
        return Observation(
            present=True,
            matches=matches,
            delete_confirmed=master.status is EventStatus.CANCELLED,
            unchanged=unchanged,
            absence_provable=True,
            receipt=receipt,
        )

    def _find_created(self, binding: CalDavBinding, prepared: Prepared) -> Observation:
        uid = prepared.reconcile_token or ""
        found: list[Observation] = []
        for name, etag, text in self._report(
            binding, self._collection(binding), dav.query_uid(uid), strict=True
        ):
            master = ics.master_of(text)
            if master.uid == uid:
                found.append(_created(master, prepared, binding, _receipt(name, etag)))
        if not found:
            return Observation(present=False, absence_provable=True)
        return found[0] if len(found) == 1 else Observation(present=True, complete=False)

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
            raise calendar_unavailable(str(error)) from None
        if reply.status == 404:
            return None
        _check(reply, (200,))
        return reply.body.decode("utf-8", "replace"), reply.headers.get("etag", "")

    def _report(
        self, binding: CalDavBinding, url: str, body: bytes, *, strict: bool = False
    ) -> list[tuple[str, str, str]]:
        """`(name, etag, calendar-data)` for every event resource the query matches."""
        try:
            reply = self._send(binding, "REPORT", url, body, {"Depth": "1"})
        except TransportFailure as error:
            raise calendar_unavailable(str(error)) from None
        _check(reply, (207,))
        try:
            responses = dav.parse_multistatus(reply.body)
        except ValueError:
            raise calendar_unavailable("unreadable multistatus") from None
        collection_path = urlsplit(self._collection(binding)).path
        found: list[tuple[str, str, str]] = []
        for response in responses:
            name = dav.name_in(collection_path, response.href)
            if name is None:
                continue
            if response.calendar_data and response.etag:
                found.append((name, response.etag, response.calendar_data))
            elif strict:
                raise calendar_unavailable("incomplete UID report")
        return found

    def _occurrence(
        self, binding: CalDavBinding, name: str, text: str, occurrence: str
    ) -> ics.ParsedEvent | None:
        override = ics.occurrence_of(text, occurrence)
        if override is not None:
            return override
        named = parse_occurrence_id(occurrence)
        if named is None:
            return None
        body = dav.query_window(named[0], named[0] + timedelta(seconds=1), expand=True)
        for other, _, expanded in self._report(binding, self._collection(binding), body):
            if other == name:
                date = ics.occurrence_of(expanded, occurrence)
                if date is not None:
                    return date
        return None


def _check(reply: HttpReply, accepted: tuple[int, ...]) -> None:
    if reply.status in accepted:
        return
    if reply.status in (401, 403):
        raise ChamberlainError(
            ErrorCode.SOURCE_SIGN_IN_NEEDED,
            "The calendar server did not accept the saved sign-in.",
            detail=f"http {reply.status}",
        )
    raise calendar_unavailable(f"http {reply.status}")


def own_address(binding: CalDavBinding) -> str | None:
    """The calendar user address this account sends and answers invitations as."""
    for candidate in (binding.address, binding.username):
        if candidate and "@" in candidate:
            return candidate.strip().lower()
    return None


def _need_address(binding: CalDavBinding) -> str:
    address = own_address(binding)
    if address is None:
        raise ics.no_invitations()
    return address


def _is_invitee_copy(parsed: ics.ParsedEvent, me: str | None) -> bool:
    """Someone else organised it (or it lists guests and says nobody did)."""
    guests = [a for a, _ in parsed.partstats if a != parsed.organiser_address]
    if parsed.organiser_address is None:
        return bool(guests)
    return parsed.organiser_address != me


def _same(parsed: ics.ParsedEvent, wanted: EventFields, binding: CalDavBinding) -> bool:
    """The read-back carries what was written, guests included (W-36) and the repeat rule when
    one was written (P-27). The organiser's own entry is ignored: servers list the organiser among
    the attendees or not."""
    owner = own_address(binding)
    if wanted.visibility is not None and parsed.organiser_address not in (None, owner):
        return False
    return (
        _rule_matches(parsed, wanted)
        and same_content(parsed.fields, wanted)
        and attendees_match(
            parsed.fields.attendees,
            wanted.attendees,
            ignore=[address for address in (owner, parsed.organiser_address) if address],
        )
    )


def _rule_matches(parsed: ics.ParsedEvent, wanted: EventFields) -> bool:
    return wanted.recurrence is None or same_rule(
        parsed.rule, wanted.recurrence, parsed.fields.start.date(), all_day=wanted.all_day
    )


def _created(
    actual: ics.ParsedEvent, prepared: Prepared, binding: CalDavBinding, receipt: ProviderReceipt
) -> Observation:
    wanted = fields(prepared)
    same_id = actual.uid == prepared.reconcile_token and actual.status is not EventStatus.CANCELLED
    return Observation(
        present=True,
        matches=same_id and _same(actual, wanted, binding),
        identity_suffices=(
            same_id
            and wanted.visibility is None
            and not wanted.attendees
            and _rule_matches(actual, wanted)
        ),
        receipt=receipt,
    )


def _event(
    source: str, locator: str, etag: str, parsed: ics.ParsedEvent, me: str | None
) -> CalendarEvent:
    invitee = bool(me is not None and _is_invitee_copy(parsed, me) and parsed.organiser_address)
    hidden = {a for a in (me, parsed.organiser_address if not invitee else None) if a}
    fields = parsed.fields.model_copy(
        update={"attendees": tuple(a for a in parsed.fields.attendees if a not in hidden)}
    )
    answers = dict(parsed.partstats)
    series = ics.split_locator(locator)[0] if parsed.recurring or parsed.recurrence_id else None
    if parsed.recurrence_id is None and parsed.rule is not None:
        fields = fields.model_copy(update={"recurrence": parsed.rule})
    return CalendarEvent(
        series_locator=series,
        source=source,
        locator=locator,
        uid=parsed.uid,
        etag=etag,
        status=parsed.status,
        fields=fields,
        updated=parsed.updated,
        organiser=parsed.organiser_address if invitee else None,
        my_response=(
            _RESPONSES.get(answers.get(me or "", ""), Response.NEEDS_ACTION)
            if invitee and me in answers
            else None
        ),
        participants=tuple(
            Participant(
                address=a, response=_RESPONSES.get(answers.get(a, ""), Response.NEEDS_ACTION)
            )
            for a in fields.attendees
        ),
    )


def _receipt(name: str, etag: str | None) -> ProviderReceipt:
    return ProviderReceipt(locator=name, etag=etag or None, verification=_VERIFIED)
