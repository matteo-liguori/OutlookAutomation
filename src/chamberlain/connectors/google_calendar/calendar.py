"""Google Calendar API backend for the shared calendar write lifecycle.

Contract: docs/contracts/08-ports.md (P-1 to P-18) and 04-writes.md. A 5xx, 408 or
429 answer to a write, or a lost connection after sending, means the write may have
happened; any other 4xx and a connection that never opened mean nothing happened.
The connector never retries a write and never reports `not_applied` on a timeout.
"""

import base64
import hashlib
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
from chamberlain.connectors.calendar_support.port import (
    CalendarPort,
    Current,
    Observation,
    WriteFailure,
    WriteReply,
    expected_etag,
    fields,
    locator,
    payload,
)
from chamberlain.connectors.google_calendar import mapping
from chamberlain.connectors.google_calendar.config import GoogleBinding, GoogleCalendarConfig
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery
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
    CalendarIntent,
    CalendarRespond,
    CalendarUpdate,
    IntentKind,
)
from chamberlain.domain.operations import Prepared, ProviderReceipt, Verification
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
        IntentKind.CALENDAR_RESPOND.value,
    }
)

_EDIT_METHODS = {
    IntentKind.CALENDAR_UPDATE: "PUT",
    IntentKind.CALENDAR_DELETE: "DELETE",
    IntentKind.CALENDAR_RESPOND: "PATCH",
}

TokenSource = Callable[[str], str]
Json = dict[str, Any]


def _quote(value: str) -> str:
    return quote(value, safe="")


def _text(data: Mapping[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "A prepared field is missing.")
    return value


class _GoogleBackend:
    """Google identity, wire format, and observations of writes."""

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
                Capability(
                    name=CapabilityName.CALENDAR_INVITE, level=VerificationLevel.IMPLEMENTED
                ),
                Capability(
                    name=CapabilityName.CALENDAR_RECURRENCE,
                    level=VerificationLevel.IMPLEMENTED,
                    absence_provable=_PROVABLE,
                ),
                Capability(
                    name=CapabilityName.CALENDAR_OCCURRENCE,
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
        body = self._read(binding, self._events_path(binding), params)
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

    def resolve(self, source: str, locator: str) -> Current:
        item = self._get_by_id(self._binding(source), locator)
        if item is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        part = (
            "occurrence"
            if item.get("recurringEventId")
            else "series"
            if item.get("recurrence")
            else None
        )
        # Only a provider etag for an active event can authorize the If-Match write.
        raw_etag = item.get("etag")
        etag = raw_etag if isinstance(raw_etag, str) and item.get("status") != "cancelled" else ""
        return Current(locator=locator, etag=etag, part=part, raw=item)

    def validate(self, intent: CalendarIntent, current: Current | None) -> None:
        if current is None:
            self._binding(intent.source)
            return
        item = cast(Json, current.raw)
        if isinstance(intent, CalendarRespond):
            if not mapping.is_invitee_copy(item) or mapping.own_entry(item) is None:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "That event is not an invitation to this calendar."
                )
        else:
            mapping.refuse_if_unwritable(item)

    def encode(self, intent: CalendarIntent, current: Current | None) -> dict[str, JsonValue]:
        if isinstance(intent, CalendarCreate):
            uid = intent.uid
            assert uid is not None  # The port requires a UID before encoding.
            if intent.event.attendees:
                event_id = event_id_for(uid)
                return {
                    "body": mapping.insert_body(event_id, intent.event),
                    "event_id": event_id,
                    "notify": True,
                }
            # Import preserves the iCalUID of a guestless appointment and sends nothing.
            return {"body": mapping.import_body(uid, intent.event)}
        assert current is not None
        before = cast(Json, current.raw)
        if isinstance(intent, CalendarUpdate):
            return {
                "body": mapping.merge_update(before, mapping.wanted_body(intent.event, before)),
                "notify": bool(intent.event.attendees) or _has_guests(before),
            }
        if isinstance(intent, CalendarRespond):
            attendees = cast("list[JsonValue]", mapping.answer_entries(before, intent.response))
            return {
                "body": {"attendees": attendees},
                "response": intent.response,
                "notify": True,
            }
        return {"notify": _has_guests(before)}

    def preflight(self, prepared: Prepared) -> None:
        binding = self._binding(prepared.source)
        if prepared.intent_kind is IntentKind.CALENDAR_CREATE:
            before = self._created(binding, prepared)
            if before is not None:
                raise ChamberlainError(
                    ErrorCode.REMOTE_CHANGED, "An event with this UID already exists."
                )

    def write(self, prepared: Prepared) -> WriteReply:
        binding = self._binding(prepared.source)
        values = payload(prepared)
        events = self._events_path(binding)
        kind = prepared.intent_kind
        notify = values.get("notify") is True
        params = {"sendUpdates": "all"} if notify else {}
        headers: dict[str, str] = {}
        raw_body = values.get("body")
        body = dict(raw_body) if isinstance(raw_body, dict) else None
        if kind is IntentKind.CALENDAR_CREATE:
            method = "POST"
            path = events if "event_id" in values else events + "/import"
        else:
            path = f"{events}/{_quote(locator(prepared))}"
            headers["If-Match"] = expected_etag(prepared)
            method = _EDIT_METHODS[kind]
        try:
            reply = self._send(binding, method, path, params=params, body=body, headers=headers)
        except ChamberlainError as error:
            # Credential acquisition happens before the transport is called.
            raise WriteFailure(str(error), sent=False) from None
        except TransportFailure as error:
            raise WriteFailure(f"transport: {error}", sent=error.sent) from None
        if reply.status not in (200, 204) and not (reply.status == 410 and method == "DELETE"):
            raise WriteFailure(f"google http {reply.status}", sent=True, status=reply.status)
        return WriteReply()

    def observe(self, prepared: Prepared, reply: WriteReply | None) -> Observation:
        binding = self._binding(prepared.source)
        kind = prepared.intent_kind
        create = kind is IntentKind.CALENDAR_CREATE
        target = None if create else locator(prepared)
        item = (
            self._created(binding, prepared) if target is None else self._get_by_id(binding, target)
        )
        if item is None:
            if kind is IntentKind.CALENDAR_DELETE and reply is not None:
                return Observation(
                    present=False,
                    delete_confirmed=True,
                    receipt=ProviderReceipt(locator=target, verification=_VERIFIED),
                )
            return Observation(present=False, absence_provable=create)
        unchanged = not create and item.get("etag") == expected_etag(prepared)
        receipt = _receipt(item)
        if item.get("status") == "cancelled":
            deleting = kind is IntentKind.CALENDAR_DELETE
            if deleting and reply is not None:
                receipt = ProviderReceipt(locator=target, verification=_VERIFIED)
            return Observation(
                present=True,
                delete_confirmed=deleting,
                unchanged=(deleting or kind is IntentKind.CALENDAR_RESPOND) and unchanged,
                absence_provable=True,
                receipt=receipt,
            )
        matches = False
        identity_suffices = False
        values = payload(prepared)
        if kind is IntentKind.CALENDAR_RESPOND:
            matches = _own_answer(item) == _text(values, "response")
        elif kind is not IntentKind.CALENDAR_DELETE:
            wanted = fields(prepared)
            body = values.get("body")
            if (
                wanted.visibility is None
                and isinstance(body, dict)
                and body.get("visibility") == "private"
            ):
                wanted = wanted.model_copy(update={"visibility": "private"})
            matches = mapping.fields_match(item, wanted, self._clock.now())
            identity_suffices = create and "event_id" not in values and wanted.visibility is None
        return Observation(
            present=True,
            matches=matches,
            identity_suffices=identity_suffices,
            unchanged=unchanged,
            absence_provable=True,
            receipt=receipt,
        )

    @staticmethod
    def _events_path(binding: GoogleBinding) -> str:
        return f"/calendars/{_quote(binding.calendar_id)}/events"

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
        path = f"{self._events_path(binding)}/{_quote(locator)}"
        try:
            reply = self._send(binding, "GET", path)
        except TransportFailure as error:
            raise _unavailable(str(error)) from None
        if reply.status in (404, 410):
            return None
        item = self._json(reply)
        if item.get("id") != locator:
            raise _unavailable("event returned a different ID")
        return item

    def _find_by_uid(self, binding: GoogleBinding, uid: str) -> Json | None:
        body = self._read(
            binding,
            self._events_path(binding),
            {"iCalUID": uid, "showDeleted": "false", "maxResults": "10"},
        )
        masters = [
            i
            for i in body.get("items", [])
            if i.get("iCalUID") == uid
            and not i.get("recurringEventId")
            and i.get("status") != "cancelled"
        ]
        if len(masters) > 1 or body.get("nextPageToken"):
            raise ChamberlainError(ErrorCode.REMOTE_AMBIGUOUS, "The calendar UID is not unique.")
        return masters[0] if masters else None

    def _created(self, binding: GoogleBinding, prepared: Prepared) -> Json | None:
        values = payload(prepared)
        if "event_id" in values:
            return self._get_by_id(binding, _text(values, "event_id"))
        uid = prepared.reconcile_token
        if uid is None:
            raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "The event UID was not stored.")
        return self._find_by_uid(binding, uid)


class GoogleCalendar(CalendarPort):
    """Calendar port backed by the Google Calendar API."""

    def __init__(
        self,
        config: GoogleCalendarConfig,
        tokens: TokenSource,
        clock: Clock,
        transport: HttpTransport | None = None,
    ) -> None:
        super().__init__(_GoogleBackend(config, tokens, clock, transport))


def event_id_for(uid: str) -> str:
    """The client-chosen id of an invitation: Google ids are 5 to 1024 characters of a-v, 0-9."""
    return hashlib.sha256(uid.encode()).hexdigest()[:32]


def _has_guests(item: Json) -> bool:
    return any(not a.get("self") for a in item.get("attendees", []))


def _own_answer(item: Json) -> str | None:
    entry = mapping.own_entry(item)
    return None if entry is None else str(entry.get("responseStatus", ""))


def _receipt(item: Json) -> ProviderReceipt:
    return ProviderReceipt(
        locator=str(item["id"]), etag=str(item.get("etag") or ""), verification=_VERIFIED
    )


def _unavailable(detail: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.SOURCE_UNAVAILABLE, "Google Calendar did not answer.", detail=detail
    )


def _encode_cursor(token: str) -> str:
    return "g1." + base64.urlsafe_b64encode(token.encode()).decode()


def _decode_cursor(cursor: str) -> str:
    try:
        if not cursor.startswith("g1."):
            raise ValueError(cursor)
        return base64.b64decode(cursor[3:], altchars=b"-_", validate=True).decode()
    except ValueError:
        raise ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid.") from None
