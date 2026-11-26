"""CalendarPort over CalDAV, for iCloud and any standard server.

Contract: docs/contracts/08-ports.md (P-1 to P-18) and 04-writes.md. Writes are one HTTP
request made conditional: `If-None-Match: *` for a create (so a repeat cannot duplicate)
and `If-Match: <etag>` for update and delete (so a changed event is never overwritten).
A 4xx answer or an unopened connection means nothing happened; a timeout, 5xx, 408 or 429
means the write may have happened and is settled only by reconcile.
"""

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from urllib.parse import quote, urlsplit

from pydantic import JsonValue

from chamberlain.connectors.caldav import dav
from chamberlain.connectors.caldav.config import CalDavBinding, CalDavConfig
from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.calendar_support.fields import (
    attendees_match,
    invite_response,
    same_content,
)
from chamberlain.connectors.calendar_support.http import (
    HttpReply,
    HttpTransport,
    UrllibTransport,
    calendar_unavailable,
    check_read_status,
    http_health,
    read_http,
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
    WriteReply,
    binding_for,
    calendar_capabilities,
    commit_http,
    conditional_headers,
    expected_etag,
    fields,
    payload,
    prepared_text,
    require_event,
    select_unique,
)
from chamberlain.connectors.calendar_support.rrule import same_rule
from chamberlain.domain.calendar import (
    CalendarEvent,
    CalendarQuery,
    EventFields,
    EventStatus,
    Participant,
)
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
from chamberlain.domain.recurrence import parse_occurrence_id
from chamberlain.ports.clock import Clock
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_VERIFIED = Verification(client_verified=True, server_verified=True)
_PARTSTAT = {"accepted": "ACCEPTED", "tentative": "TENTATIVE", "declined": "DECLINED"}
_CALENDAR_TYPE = {"Content-Type": 'text/calendar; charset="utf-8"'}
_XML_TYPE = {"Content-Type": "application/xml; charset=utf-8"}

PasswordSource = Callable[[CalDavBinding], str]


def resource_name(uid: str) -> str:
    """The file a create writes: derived from the uid, so a repeat hits the same resource."""
    return sha256_text(uid)[:40] + ".ics"


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
        return calendar_capabilities(
            source, server=True, invitations=own_address(self._binding(source)) is not None
        )

    def health(self, source: str) -> SourceHealth:
        binding = self._binding(source)
        return http_health(
            source,
            self._clock.now(),
            lambda: self._send(
                binding, "PROPFIND", self._collection(binding), dav.PROPFIND_HEALTH, {"Depth": "0"}
            ),
            207,
        )

    def events(
        self, source: str, query: CalendarQuery, cursor: str | None = None
    ) -> Page[CalendarEvent]:
        binding = self._binding(source)
        offset = offset_cursor(cursor, "c1")
        found: list[CalendarEvent] = []
        gaps: list[Gap] = []
        body = dav.query_window(query.start, query.end, expand=True)
        me = own_address(binding)
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
                        me,
                    )
                )
        return offset_page(found, gaps, offset, query.limit, "c1")

    def read(self, source: str, locator: str) -> CalendarEvent:
        binding = self._binding(source)
        name, occurrence, text, etag = self._load(binding, locator)
        parsed = (
            ics.master_of(text)
            if occurrence is None
            else require_event(self._occurrence(binding, name, text, occurrence), occurrence=True)
        )
        return _event(
            source, name if occurrence is None else locator, etag, parsed, own_address(binding)
        )

    # --- writing --------------------------------------------------------------

    def resolve(self, source: str, locator: str) -> Current:
        binding = self._binding(source)
        name, occurrence, text, etag = self._load(binding, locator)
        master = ics.master_of(text)
        if occurrence is not None:
            require_event(self._occurrence(binding, name, text, occurrence), occurrence=True)
        return Current(
            locator=locator,
            etag=etag,
            part="occurrence" if occurrence else "series" if master.recurring else None,
            raw=_Resource(name, text, master, occurrence),
        )

    def encode(self, intent: CalendarIntent, current: Current | None) -> dict[str, JsonValue]:
        binding = self._binding(intent.source)
        now = self._clock.now()
        if isinstance(intent, CalendarCreate):
            assert intent.uid is not None
            organiser = _need_address(binding) if intent.event.attendees else None
            return {
                "name": resource_name(intent.uid),
                "ics": ics.build_new(intent.uid, intent.event, now, organiser),
            }
        assert current is not None
        resource = cast(_Resource, current.raw)
        occurrence = resource.occurrence
        master = resource.master
        data: dict[str, JsonValue] = {"name": resource.name, "occurrence": occurrence}
        if isinstance(intent, CalendarRespond):
            me = _need_address(binding)
            if occurrence is not None:
                raise ChamberlainError(
                    ErrorCode.CAPABILITY_UNSUPPORTED,
                    "A single occurrence of a recurring event cannot be answered.",
                )
            if not _is_invitee_copy(master, me) or me not in dict(master.partstats):
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "That event is not an invitation to this calendar."
                )
            data.update(
                ics=ics.apply_answer(resource.text, me, _PARTSTAT[intent.response], now),
                response=intent.response,
                address=me,
            )
            return data
        if _is_invitee_copy(master, own_address(binding)):
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "Only the organiser can change that event.",
                detail="invitee-copy",
            )
        if isinstance(intent, CalendarUpdate):
            organiser = (
                _need_address(binding)
                if intent.event.attendees or master.fields.attendees
                else None
            )
            data["ics"] = (
                ics.apply_occurrence(resource.text, occurrence, intent.event, now)
                if occurrence is not None
                else ics.apply_fields(resource.text, intent.event, now, organiser)
            )
        elif occurrence is not None:
            data["ics"] = ics.cancel_occurrence(resource.text, occurrence, now)
        return data

    def write(self, prepared: Prepared) -> WriteReply:
        return commit_http(lambda: self._request(prepared), "caldav")

    def _request(self, prepared: Prepared) -> tuple[Callable[[], HttpReply], tuple[int, ...]]:
        data = payload(prepared)
        name = prepared_text(data, "name")
        headers = conditional_headers(prepared, exclusive_create=True)
        if prepared.intent_kind is IntentKind.CALENDAR_DELETE and "ics" not in data:
            method, body = "DELETE", None
        else:
            method, body = "PUT", prepared_text(data, "ics").encode("utf-8")
            headers.update(_CALENDAR_TYPE)
        binding = self._binding(prepared.source)
        target = self._resource(binding, name)
        return lambda: self._send(binding, method, target, body, headers), (200, 201, 204)

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
            deleted = name if kind is IntentKind.CALENDAR_DELETE and occurrence is None else None
            return Observation.missing(deleted=deleted)
        text, etag = fetched
        master = ics.master_of(text)
        receipt = _receipt(name, etag)
        if kind is IntentKind.CALENDAR_CREATE:
            return _created(master, prepared, binding, receipt)
        present = True
        deleted = master.status is EventStatus.CANCELLED
        matches = False
        if isinstance(occurrence, str):
            actual = ics.occurrence_of(text, occurrence)
            deleted = (actual is None and occurrence in master.cancelled_dates) or (
                actual is not None and actual.status is EventStatus.CANCELLED
            )
            present = not deleted
            if kind is IntentKind.CALENDAR_UPDATE and actual is not None:
                # The occurrence update preserves the series' guests. Compare the complete
                # encoded override, including any private classification and those guests.
                wanted = ics.occurrence_of(prepared_text(data, "ics"), occurrence)
                matches = wanted is not None and _same(actual, wanted.fields, binding)
        elif kind is IntentKind.CALENDAR_RESPOND:
            answer = dict(master.partstats).get(prepared_text(data, "address"))
            matches = answer == _PARTSTAT[prepared_text(data, "response")]
        elif kind is IntentKind.CALENDAR_UPDATE:
            wanted = fields(prepared)
            if wanted.recurrence is None:
                # A series update that supplies no rule must keep the original rule.
                encoded = ics.master_of(prepared_text(data, "ics"))
                wanted = wanted.model_copy(update={"recurrence": encoded.rule})
            matches = _same(master, wanted, binding)
        return Observation(
            present=present,
            matches=matches,
            delete_confirmed=deleted,
            unchanged=etag == expected_etag(prepared),
            absence_provable=True,
            receipt=receipt,
        )

    def _find_created(self, binding: CalDavBinding, prepared: Prepared) -> Observation:
        uid = prepared.reconcile_token or ""
        resources = self._report(
            binding, self._collection(binding), dav.query_uid(uid), strict=True
        )
        match, unique = select_unique(
            (name, etag, master)
            for name, etag, text in resources
            if (master := ics.master_of(text)).uid == uid
        )
        if not unique:
            return Observation(present=True, complete=False)
        if match is None:
            return Observation.missing(proof=True)
        name, etag, master = match
        return _created(master, prepared, binding, _receipt(name, etag))

    # --- transport ------------------------------------------------------------

    def _binding(self, source: str) -> CalDavBinding:
        return binding_for(self._config.bindings, source)

    @staticmethod
    def _collection(binding: CalDavBinding) -> str:
        return binding.url if binding.url.endswith("/") else binding.url + "/"

    def _resource(self, binding: CalDavBinding, name: str) -> str:
        return self._collection(binding) + quote(name, safe="")

    def _load(self, binding: CalDavBinding, locator: str) -> tuple[str, str | None, str, str]:
        name, occurrence = ics.split_locator(locator)
        text, etag = require_event(self._fetch(binding, name))
        return name, occurrence, text, etag

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
        reply = read_http(
            lambda: self._send(binding, "GET", self._resource(binding, name)), calendar_unavailable
        )
        if reply.status == 404:
            return None
        _check(reply, 200)
        return reply.body.decode("utf-8", "replace"), reply.headers.get("etag", "")

    def _report(
        self, binding: CalDavBinding, url: str, body: bytes, *, strict: bool = False
    ) -> list[tuple[str, str, str]]:
        """`(name, etag, calendar-data)` for every event resource the query matches."""
        reply = read_http(
            lambda: self._send(binding, "REPORT", url, body, {"Depth": "1"}), calendar_unavailable
        )
        _check(reply, 207)
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


def _check(reply: HttpReply, accepted: int) -> None:
    check_read_status(
        reply,
        accepted,
        calendar_unavailable,
        "The calendar server did not accept the saved sign-in.",
    )


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
    return ics.event_view(
        parsed,
        source,
        locator,
        etag,
        fields=fields,
        organiser=parsed.organiser_address if invitee else None,
        my_response=(
            invite_response(answers[me].lower())
            if invitee and me is not None and me in answers
            else None
        ),
        participants=tuple(
            Participant(address=a, response=invite_response(answers.get(a, "").lower()))
            for a in fields.attendees
        ),
    )


def _receipt(name: str, etag: str | None) -> ProviderReceipt:
    return ProviderReceipt(locator=name, etag=etag or None, verification=_VERIFIED)
