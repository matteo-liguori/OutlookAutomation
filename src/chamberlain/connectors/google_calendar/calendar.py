"""CalendarPort over the Google Calendar API.

Contract: docs/contracts/08-ports.md (P-1 to P-18) and 04-writes.md. A 5xx, 408 or
429 answer to a write, or a lost connection after sending, means the write may have
happened; any other 4xx and a connection that never opened mean nothing happened.
The connector never retries a write and never reports `not_applied` on a timeout.
"""

import base64
import json
from collections.abc import Callable, Mapping
from typing import Any, Final, cast
from urllib.parse import quote, urlencode

from pydantic import JsonValue

from chamberlain.connectors.calendar_support.http import (
    HttpReply,
    HttpTransport,
    TransportFailure,
    UrllibTransport,
)
from chamberlain.connectors.google_calendar import mapping
from chamberlain.connectors.google_calendar.config import GoogleBinding, GoogleCalendarConfig
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery, EventFields
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
from chamberlain.ports.page import Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode

BASE: Final = "https://www.googleapis.com/calendar/v3"
_VERIFIED = Verification(client_verified=True, server_verified=True)
_PROVABLE = frozenset(
    {
        IntentKind.CALENDAR_CREATE.value,
        IntentKind.CALENDAR_UPDATE.value,
        IntentKind.CALENDAR_DELETE.value,
    }
)
_MAY_HAVE_LANDED = frozenset({408, 429})

TokenSource = Callable[[str], str]
Json = dict[str, Any]


def _quote(value: str) -> str:
    return quote(value, safe="")


def may_have_landed(status: int) -> bool:
    """A write answer that does not prove the server refused it."""
    return status >= 500 or status in _MAY_HAVE_LANDED or status < 400


def _text(data: Mapping[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "A prepared field is missing.")
    return value


class GoogleCalendar:
    """Implements `chamberlain.ports.CalendarPort` for Google calendars."""

    def __init__(
        self,
        config: GoogleCalendarConfig,
        tokens: TokenSource,
        clock: Clock,
        transport: HttpTransport | None = None,
    ) -> None:
        self._config = config
        self._tokens = tokens
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
            reply = self._send(binding, "GET", f"/calendars/{_quote(binding.calendar_id)}")
        except ChamberlainError as error:
            state = (
                HealthState.SIGN_IN_NEEDED
                if error.code is ErrorCode.SOURCE_SIGN_IN_NEEDED
                else HealthState.UNAVAILABLE
            )
            return SourceHealth(source=source, state=state, checked_at=now)
        except TransportFailure:
            return SourceHealth(source=source, state=HealthState.UNAVAILABLE, checked_at=now)
        if reply.status == 200:
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
        params: dict[str, str] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "showDeleted": "true" if query.include_cancelled else "false",
            "maxResults": str(query.limit),
            **mapping.window_params(query.start, query.end),
        }
        if query.text:
            params["q"] = query.text
        if cursor is not None:
            params["pageToken"] = _decode_cursor(cursor)
        body = self._read(binding, f"/calendars/{_quote(binding.calendar_id)}/events", params)
        now = self._clock.now()
        items = [
            mapping.event_of(source, item, now)
            for item in body.get("items", [])
            if mapping.has_start(item)
            and (query.include_cancelled or item.get("status") != "cancelled")
        ]
        token = body.get("nextPageToken")
        return Page[CalendarEvent](
            items=tuple(items),
            cursor=_encode_cursor(str(token)) if token else None,
            complete=not token,
        )

    def read(self, source: str, locator: str) -> CalendarEvent:
        binding = self._binding(source)
        item = self._get_by_id(binding, locator)
        if item is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        return mapping.event_of(source, item, self._clock.now())

    # --- writing --------------------------------------------------------------

    def prepare(self, intent: CalendarIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only (P-9). Builds the exact request body `dispatch` will send."""
        binding = self._binding(intent.source)
        data: dict[str, JsonValue]
        token: str | None = None
        match intent:
            case CalendarCreate():
                if intent.uid is None:
                    raise ChamberlainError(
                        ErrorCode.REQUEST_INVALID, "The event has no uid.", field="uid"
                    )
                _refuse_attendees(intent.event)
                token = intent.uid
                body = mapping.import_body(intent.uid, intent.event)
                data = {"body": body, "fields": intent.event.model_dump(mode="json")}
            case CalendarUpdate():
                _refuse_attendees(intent.event)
                before = self._current(binding, refs, intent.event_ref, intent.expected_etag)
                body = mapping.merge_update(before, mapping.wanted_body(intent.event, before))
                data = {
                    "locator": str(before["id"]),
                    "expected_etag": intent.expected_etag,
                    "body": body,
                    "fields": intent.event.model_dump(mode="json"),
                }
            case CalendarDelete():
                before = self._current(binding, refs, intent.event_ref, intent.expected_etag)
                data = {"locator": str(before["id"]), "expected_etag": intent.expected_etag}
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
            binding = self._binding(prepared.source)
            return self._dispatch(binding, prepared)
        except ChamberlainError as error:
            # Raised before anything was sent (sign-in, configuration): nothing happened.
            return DispatchResult(
                outcome=DispatchOutcome.NOT_APPLIED, detail=f"{error.code}: {error.detail}"
            )

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11). Settles only from a read-back."""
        try:
            binding = self._binding(prepared.source)
            return self._reconcile(binding, prepared)
        except (ChamberlainError, TransportFailure) as error:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail=_why(error))

    # --- dispatch -------------------------------------------------------------

    def _dispatch(self, binding: GoogleBinding, prepared: Prepared) -> DispatchResult:
        data = prepared.data
        events = f"/calendars/{_quote(binding.calendar_id)}/events"
        kind = prepared.intent_kind
        if kind is IntentKind.CALENDAR_CREATE:
            uid = prepared.reconcile_token or ""
            try:
                existing = self._find_by_uid(binding, uid)
            except (ChamberlainError, TransportFailure) as error:
                return DispatchResult(outcome=DispatchOutcome.NOT_APPLIED, detail=_why(error))
            if existing is not None:
                return DispatchResult(
                    outcome=DispatchOutcome.NOT_APPLIED, detail="an event with this uid exists"
                )
            method, path, headers = "POST", events + "/import", {}
            body: Json | None = dict(data["body"]) if isinstance(data["body"], dict) else None
        else:
            path = f"{events}/{_quote(_text(data, 'locator'))}"
            headers = {"If-Match": _text(data, "expected_etag")}
            if kind is IntentKind.CALENDAR_DELETE:
                method, body = "DELETE", None
            else:
                method = "PUT"
                body = dict(data["body"]) if isinstance(data["body"], dict) else None
        try:
            reply = self._send(binding, method, path, body=body, headers=headers)
        except TransportFailure as error:
            outcome = DispatchOutcome.UNKNOWN if error.sent else DispatchOutcome.NOT_APPLIED
            return DispatchResult(outcome=outcome, detail=f"transport: {error}")
        if reply.status not in (200, 204, 410) or (reply.status == 410 and method != "DELETE"):
            outcome = (
                DispatchOutcome.UNKNOWN
                if may_have_landed(reply.status)
                else DispatchOutcome.NOT_APPLIED
            )
            return DispatchResult(outcome=outcome, detail=f"google http {reply.status}")
        return self._verify(binding, prepared, reply)

    def _verify(
        self, binding: GoogleBinding, prepared: Prepared, reply: HttpReply
    ) -> DispatchResult:
        """The write was accepted. Read it back; an unreadable or different result is unknown."""
        kind = prepared.intent_kind
        now = self._clock.now()
        try:
            if kind is IntentKind.CALENDAR_CREATE:
                after = self._find_by_uid(binding, prepared.reconcile_token or "")
            else:
                after = self._get_by_id(binding, _text(prepared.data, "locator"))
        except (ChamberlainError, TransportFailure) as error:
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail=f"not verified: {_why(error)}"
            )
        if kind is IntentKind.CALENDAR_DELETE:
            if after is None or after.get("status") == "cancelled":
                receipt = ProviderReceipt(
                    locator=_text(prepared.data, "locator"), verification=_VERIFIED
                )
                return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=receipt)
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="still present")
        wanted = EventFields.model_validate(prepared.data["fields"])
        if after is None or not mapping.fields_match(after, wanted, now):
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail="read-back differs from the request"
            )
        return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=_receipt(after))

    # --- reconcile ------------------------------------------------------------

    def _reconcile(self, binding: GoogleBinding, prepared: Prepared) -> ReconcileResult:
        kind = prepared.intent_kind
        now = self._clock.now()
        if kind is IntentKind.CALENDAR_CREATE:
            found = self._find_by_uid(binding, prepared.reconcile_token or "")
            if found is None:
                return ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)
            return _present(found)
        current = self._get_by_id(binding, _text(prepared.data, "locator"))
        if current is None:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="event not found")
        gone = current.get("status") == "cancelled"
        unchanged = current.get("etag") == _text(prepared.data, "expected_etag")
        if kind is IntentKind.CALENDAR_DELETE:
            if gone:
                return _present(current)
            if unchanged:
                return ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="changed otherwise")
        if unchanged and not gone:
            return ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)
        wanted = EventFields.model_validate(prepared.data["fields"])
        if not gone and mapping.fields_match(current, wanted, now):
            return _present(current)
        return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="changed otherwise")

    # --- transport ------------------------------------------------------------

    def _binding(self, source: str) -> GoogleBinding:
        binding = self._config.bindings.get(source)
        if binding is None:
            raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, "That source is not set up.")
        return binding

    def _send(
        self,
        binding: GoogleBinding,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Json | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpReply:
        url = BASE + path + (("?" + urlencode(params)) if params else "")
        sent = {
            "Authorization": f"Bearer {self._tokens(binding.account)}",
            "Accept": "application/json",
            **(headers or {}),
        }
        payload = None
        if body is not None:
            sent["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        return self._transport.send(
            method, url, headers=sent, body=payload, timeout=self._config.deadline_seconds
        )

    def _read(self, binding: GoogleBinding, path: str, params: Mapping[str, str]) -> Json:
        """A read that must succeed; failures become `source.*` errors (P-17, P-18)."""
        try:
            reply = self._send(binding, "GET", path, params=params)
        except TransportFailure as error:
            raise _unavailable(str(error)) from None
        return self._json(reply)

    def _json(self, reply: HttpReply) -> Json:
        if reply.status in (401, 403):
            raise ChamberlainError(
                ErrorCode.SOURCE_SIGN_IN_NEEDED,
                "Google did not accept the saved sign-in.",
                detail=f"http {reply.status}",
            )
        if reply.status == 400 and b"pageToken" in reply.body:
            raise ChamberlainError(ErrorCode.CURSOR_EXPIRED, "That page is no longer available.")
        if reply.status != 200:
            raise _unavailable(f"http {reply.status}")
        try:
            data: object = json.loads(reply.body)
        except ValueError:
            raise _unavailable("unreadable body") from None
        return cast(Json, data) if isinstance(data, dict) else {}

    def _get_by_id(self, binding: GoogleBinding, locator: str) -> Json | None:
        """The event, a cancelled one as cancelled, or None when Google has no such event."""
        path = f"/calendars/{_quote(binding.calendar_id)}/events/{_quote(locator)}"
        try:
            reply = self._send(binding, "GET", path)
        except TransportFailure as error:
            raise _unavailable(str(error)) from None
        if reply.status in (404, 410):
            return None
        return self._json(reply)

    def _find_by_uid(self, binding: GoogleBinding, uid: str) -> Json | None:
        body = self._read(
            binding,
            f"/calendars/{_quote(binding.calendar_id)}/events",
            {"iCalUID": uid, "showDeleted": "false", "maxResults": "10"},
        )
        masters = [
            i
            for i in body.get("items", [])
            if not i.get("recurringEventId") and i.get("status") != "cancelled"
        ]
        return masters[0] if masters else None

    def _current(
        self, binding: GoogleBinding, refs: Mapping[str, str], ref: str, expected_etag: str
    ) -> Json:
        locator = refs.get(ref)
        if locator is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not known.")
        item = self._get_by_id(binding, locator)
        if item is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        if item.get("etag") != expected_etag or item.get("status") == "cancelled":
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The event changed since it was read.")
        mapping.refuse_if_unwritable(item)
        return item


def _refuse_attendees(fields: EventFields) -> None:
    if fields.attendees:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "Events with attendees cannot be written by this connector.",
            detail="invitations-not-supported",
        )


def _receipt(item: Json) -> ProviderReceipt:
    return ProviderReceipt(
        locator=str(item["id"]), etag=str(item.get("etag") or ""), verification=_VERIFIED
    )


def _present(item: Json) -> ReconcileResult:
    return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=_receipt(item))


def _unavailable(detail: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.SOURCE_UNAVAILABLE, "Google Calendar did not answer.", detail=detail
    )


def _why(error: Exception) -> str:
    return error.detail if isinstance(error, ChamberlainError) else str(error)


def _encode_cursor(token: str) -> str:
    return "g1." + base64.urlsafe_b64encode(token.encode()).decode()


def _decode_cursor(cursor: str) -> str:
    try:
        if not cursor.startswith("g1."):
            raise ValueError(cursor)
        return base64.b64decode(cursor[3:], altchars=b"-_", validate=True).decode()
    except ValueError:
        raise ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid.") from None
