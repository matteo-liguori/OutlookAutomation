"""Google Calendar API backend for the shared calendar write lifecycle.

Contract: docs/contracts/08-ports.md (P-1 to P-18) and 04-writes.md. A 5xx, 408 or
429 answer to a write, or a lost connection after sending, means the write may have
happened; any other 4xx and a connection that never opened mean nothing happened.
The connector never retries a write and never reports `not_applied` on a timeout.
"""

import json
from collections.abc import Callable, Mapping
from functools import partial
from typing import Final, cast
from urllib.parse import quote, urlencode

from pydantic import JsonValue

from chamberlain.connectors.calendar_support.http import (
    HttpReply,
    HttpTransport,
    UrllibTransport,
    calendar_unavailable,
    check_read_status,
    http_health,
    read_http,
)
from chamberlain.connectors.calendar_support.paging import decode_token, encode_token
from chamberlain.connectors.calendar_support.port import (
    CalendarWritePort,
    Current,
    Observation,
    WriteFailure,
    WriteReply,
    binding_for,
    calendar_capabilities,
    commit_http,
    conditional_headers,
    expected_etag,
    fields,
    locator,
    payload,
    prepared_text,
    require_event,
    select_unique,
)
from chamberlain.connectors.google_calendar import mapping
from chamberlain.connectors.google_calendar.config import GoogleBinding, GoogleCalendarConfig
from chamberlain.connectors.google_calendar.mapping import Resource
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.capabilities import (
    CapabilitySet,
    SourceHealth,
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
_EDIT_METHODS = {
    IntentKind.CALENDAR_UPDATE: "PUT",
    IntentKind.CALENDAR_DELETE: "DELETE",
    IntentKind.CALENDAR_RESPOND: "PATCH",
}

TokenSource = Callable[[str], str]

_unavailable = partial(calendar_unavailable, message="Google Calendar did not answer.")


def _quote(value: str) -> str:
    return quote(value, safe="")


class GoogleCalendar(CalendarWritePort):
    """Calendar port backed by the Google Calendar API."""

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
        return calendar_capabilities(source, server=True, invitations=True)

    def health(self, source: str) -> SourceHealth:
        binding = self._binding(source)
        return http_health(
            source,
            self._clock.now(),
            lambda: self._send(binding, "GET", f"/calendars/{_quote(binding.calendar_id)}"),
            200,
        )

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
            params["pageToken"] = decode_token(cursor, "g1")
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
            cursor=encode_token(str(token), "g1") if token else None,
            complete=not token,
        )

    def read(self, source: str, locator: str) -> CalendarEvent:
        item = require_event(self._get_by_id(self._binding(source), locator))
        return mapping.event_of(source, item, self._clock.now())

    # --- writing --------------------------------------------------------------

    def resolve(self, source: str, locator: str) -> Current:
        item = require_event(self._get_by_id(self._binding(source), locator))
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

    def encode(self, intent: CalendarIntent, current: Current | None) -> dict[str, JsonValue]:
        if isinstance(intent, CalendarCreate):
            self._binding(intent.source)
            assert intent.uid is not None
            if intent.event.attendees:
                event_id = event_id_for(intent.uid)
                return {
                    "body": mapping.insert_body(event_id, intent.event),
                    "event_id": event_id,
                    "notify": True,
                }
            # Import preserves the iCalUID of a guestless appointment and sends nothing.
            return {"body": mapping.import_body(intent.uid, intent.event)}
        assert current is not None
        before = cast(Resource, current.raw)
        if isinstance(intent, CalendarRespond):
            if not mapping.is_invitee_copy(before) or mapping.own_entry(before) is None:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "That event is not an invitation to this calendar."
                )
            return {
                "body": {
                    "attendees": cast(
                        "list[JsonValue]", mapping.answer_entries(before, intent.response)
                    )
                },
                "response": intent.response,
                "notify": True,
            }
        mapping.refuse_if_unwritable(before)
        notify = mapping.has_guests(before)
        if isinstance(intent, CalendarUpdate):
            return {
                "body": mapping.merge_update(before, mapping.wanted_body(intent.event, before)),
                "notify": bool(intent.event.attendees) or notify,
            }
        return {"notify": notify}

    def preflight(self, prepared: Prepared) -> None:
        binding = self._binding(prepared.source)
        if prepared.intent_kind is IntentKind.CALENDAR_CREATE:
            before = self._created(binding, prepared)
            if before is not None:
                raise ChamberlainError(
                    ErrorCode.REMOTE_CHANGED, "An event with this UID already exists."
                )

    def write(self, prepared: Prepared) -> WriteReply:
        try:
            binding = self._binding(prepared.source)
            values = payload(prepared)
            events = self._events_path(binding)
            kind = prepared.intent_kind
            notify = values.get("notify") is True
            params = {"sendUpdates": "all"} if notify else {}
            headers = conditional_headers(prepared)
            raw_body = values.get("body")
            body = dict(raw_body) if isinstance(raw_body, dict) else None
            if kind is IntentKind.CALENDAR_CREATE:
                method = "POST"
                path = events if "event_id" in values else events + "/import"
            else:
                path = f"{events}/{_quote(locator(prepared))}"
                method = _EDIT_METHODS[kind]
        except Exception as error:
            raise WriteFailure(type(error).__name__, sent=False) from error
        return commit_http(
            lambda: self._send(binding, method, path, params=params, body=body, headers=headers),
            "google",
            (200, 204, 410) if method == "DELETE" else (200, 204),
        )

    def observe(self, prepared: Prepared, reply: WriteReply | None) -> Observation:
        binding = self._binding(prepared.source)
        kind = prepared.intent_kind
        create = kind is IntentKind.CALENDAR_CREATE
        target = None if create else locator(prepared)
        item = (
            self._created(binding, prepared) if target is None else self._get_by_id(binding, target)
        )
        if item is None:
            deleted = target if kind is IntentKind.CALENDAR_DELETE and reply is not None else None
            return Observation.missing(deleted=deleted, proof=create)
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
            matches = _own_answer(item) == prepared_text(values, "response")
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
        return binding_for(self._config.bindings, source)

    def _send(
        self,
        binding: GoogleBinding,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Resource | None = None,
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

    def _read(self, binding: GoogleBinding, path: str, params: Mapping[str, str]) -> Resource:
        """A read that must succeed; failures become `source.*` errors (P-17, P-18)."""
        reply = read_http(lambda: self._send(binding, "GET", path, params=params), _unavailable)
        return self._json(reply)

    def _json(self, reply: HttpReply) -> Resource:
        if reply.status == 400 and b"pageToken" in reply.body:
            raise ChamberlainError(ErrorCode.CURSOR_EXPIRED, "That page is no longer available.")
        check_read_status(reply, 200, _unavailable, "Google did not accept the saved sign-in.")
        try:
            data: object = json.loads(reply.body)
        except ValueError:
            raise _unavailable("unreadable body") from None
        return cast(Resource, data) if isinstance(data, dict) else {}

    def _get_by_id(self, binding: GoogleBinding, locator: str) -> Resource | None:
        """The event, a cancelled one as cancelled, or None when Google has no such event."""
        path = f"{self._events_path(binding)}/{_quote(locator)}"
        reply = read_http(lambda: self._send(binding, "GET", path), _unavailable)
        if reply.status in (404, 410):
            return None
        item = self._json(reply)
        if item.get("id") != locator:
            raise _unavailable("event returned a different ID")
        return item

    def _find_by_uid(self, binding: GoogleBinding, uid: str) -> Resource | None:
        body = self._read(
            binding,
            self._events_path(binding),
            {"iCalUID": uid, "showDeleted": "false", "maxResults": "10"},
        )
        master, unique = select_unique(
            i
            for i in body.get("items", [])
            if i.get("iCalUID") == uid
            and not i.get("recurringEventId")
            and i.get("status") != "cancelled"
        )
        if not unique or body.get("nextPageToken"):
            raise ChamberlainError(ErrorCode.REMOTE_AMBIGUOUS, "The calendar UID is not unique.")
        return master

    def _created(self, binding: GoogleBinding, prepared: Prepared) -> Resource | None:
        values = payload(prepared)
        if "event_id" in values:
            return self._get_by_id(binding, prepared_text(values, "event_id"))
        uid = prepared.reconcile_token
        if uid is None:
            raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "The event UID was not stored.")
        return self._find_by_uid(binding, uid)


def event_id_for(uid: str) -> str:
    """The client-chosen id of an invitation: Google ids are 5 to 1024 characters of a-v, 0-9."""
    return sha256_text(uid)[:32]


def _own_answer(item: Resource) -> str | None:
    entry = mapping.own_entry(item)
    return None if entry is None else str(entry.get("responseStatus", ""))


def _receipt(item: Resource) -> ProviderReceipt:
    return ProviderReceipt(
        locator=str(item["id"]), etag=str(item.get("etag") or ""), verification=_VERIFIED
    )
