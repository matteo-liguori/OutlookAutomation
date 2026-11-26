"""CalendarPort for the Microsoft 365 calendar through Thunderbird with Owl.

Contract: docs/contracts/08-ports.md and docs/contracts/12-bridge-v2.md (calendar commands
are an optional capability). Mail and calendar use the same typed bridge client.
Thunderbird keeps no version of an event, so the etag is a hash of its iCalendar text.
The capability set is narrow: no invitations (not declared, so a write that reaches other
people is refused when proposed; the add-on has no documented, tested way to send, change,
cancel or answer an invitation through Owl), no repeating-event writes (a series is read, as
its occurrences, but `calendar.recurrence` and `calendar.occurrence` are not declared: RRULE and
EXDATE handling through the add-on and Exchange cannot be checked here), and absence is never
proven (the client calendar is a cache, so `server_verified` is always False).
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import Field, JsonValue

from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.calendar_support.fields import same_content
from chamberlain.connectors.calendar_support.http import calendar_unavailable
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
    payload,
)
from chamberlain.connectors.thunderbird import protocol as wire
from chamberlain.connectors.thunderbird.client import BridgeClient, BridgeFailure, typed
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
    CalendarIntent,
    CalendarRespond,
    CalendarUpdate,
)
from chamberlain.domain.operations import (
    Prepared,
    ProviderReceipt,
    Verification,
)
from chamberlain.ports.clock import Clock
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.base import Model, SourceId
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_CLIENT_ONLY = Verification(client_verified=True, server_verified=False)
_SCAN_CAP = 5000

Json = dict[str, JsonValue]


class FreshnessProbe(Protocol):
    def __call__(self, source: str, token: str, *, cleanup_only: bool = False) -> bool: ...


class FreshnessStore(Protocol):
    def __call__(
        self, source: str, binding: str, health: SourceHealth, probe: FreshnessProbe
    ) -> SourceHealth: ...


class OwlBinding(Model):
    calendar_id: str = Field(min_length=1)
    """The Thunderbird calendar id."""


class OwlCalendarConfig(Model):
    bindings: dict[SourceId, OwlBinding]


class ThunderbirdCalendar(CalendarWritePort):
    """Implements `chamberlain.ports.CalendarPort` over the Thunderbird calendar bridge."""

    def __init__(
        self,
        config: OwlCalendarConfig,
        bridge: BridgeClient,
        clock: Clock,
        *,
        freshness_store: FreshnessStore | None = None,
    ) -> None:
        self._config = config
        self._bridge = bridge
        self._clock = clock
        self._freshness_store = freshness_store

    # --- description ----------------------------------------------------------

    def capabilities(self, source: str) -> CapabilitySet:
        self._binding(source)
        try:
            usable = self._compatible(self._bridge.hello())
        except BridgeFailure as failure:
            raise calendar_unavailable(
                str(failure),
                message="Thunderbird did not answer for the calendar.",
                fix="Install the current Chamberlain Thunderbird add-on and restart Thunderbird.",
            ) from None
        if not usable:
            return CapabilitySet(source=source, capabilities=())
        return CapabilitySet(
            source=source,
            capabilities=(
                Capability(
                    name=CapabilityName.CALENDAR_READ,
                    level=VerificationLevel.IMPLEMENTED,
                    limits={"page_size": 250},
                ),
                Capability(name=CapabilityName.CALENDAR_WRITE, level=VerificationLevel.IMPLEMENTED),
            ),
        )

    def health(self, source: str) -> SourceHealth:
        binding = self._binding(source)
        now = self._clock.now()
        try:
            if not self._compatible(self._bridge.hello()):
                return SourceHealth(source=source, state=HealthState.UNAVAILABLE, checked_at=now)
            listing = self._call(
                wire.CalendarListResult, wire.Command.CALENDAR_LIST, wire.StatusArgs()
            )
        except BridgeFailure:
            return SourceHealth(source=source, state=HealthState.UNAVAILABLE, checked_at=now)
        mine = next((c for c in listing.calendars if c.id == binding.calendar_id), None)
        if mine is None or mine.disabled:
            return SourceHealth(source=source, state=HealthState.UNAVAILABLE, checked_at=now)
        fresh = mine.fresh_as_of
        if self._freshness_store is not None and mine.type == "owl" and mine.read_only is False:
            stale = SourceHealth(source=source, state=HealthState.STALE, checked_at=now)
            return self._freshness_store(source, binding.calendar_id, stale, self.probe)
        if not isinstance(fresh, str):
            # A client cache that cannot say when it last synchronised cannot be vouched for (P-5).
            return SourceHealth(source=source, state=HealthState.STALE, checked_at=now)
        try:
            fresh_as_of = datetime.fromisoformat(fresh)
        except ValueError:
            return SourceHealth(source=source, state=HealthState.STALE, checked_at=now)
        return SourceHealth(
            source=source, state=HealthState.OK, checked_at=now, fresh_as_of=fresh_as_of
        )

    # --- reading --------------------------------------------------------------

    def events(
        self, source: str, query: CalendarQuery, cursor: str | None = None
    ) -> Page[CalendarEvent]:
        binding = self._binding(source)
        offset = offset_cursor(cursor, "o1")
        wanted = min(offset + query.limit + 1, _SCAN_CAP)
        result = self._query(binding, query.start, query.end, wanted)
        found: list[CalendarEvent] = []
        unreadable = 0
        for item in result.items:
            text = item.icalendar
            try:
                event = _event(source, item, text)
            except ChamberlainError:
                unreadable += 1
                continue
            if matches_window(query, event.fields, event.status):
                found.append(event)
        gaps = [Gap(source=source, reason=str(g)) for g in result.gaps]
        if unreadable:
            gaps.append(Gap(source=source, reason="Some calendar items could not be read."))
        returned = len(result.items)
        # `complete: false` at exactly the requested limit only means "there is more" (paging);
        # it is a coverage gap when the add-on stopped short of the limit or the limit is the cap.
        paging = returned >= wanted and wanted < _SCAN_CAP
        if not result.complete and not gaps and not paging:
            gaps.append(Gap(source=source, reason="The calendar did not report a complete scan."))
        if returned >= wanted and wanted >= _SCAN_CAP:
            gaps.append(Gap(source=source, reason="The scan stopped at its limit."))
        return offset_page(found, gaps, offset, query.limit, "o1")

    def read(self, source: str, locator: str) -> CalendarEvent:
        binding = self._binding(source)
        event_id, recurrence = ics.split_locator(locator)
        item = self._item(binding, event_id)
        if item is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        text = item.icalendar
        if recurrence is None:
            parsed = ics.master_of(text)
            return _event_of(source, event_id, text, parsed)
        for parsed in ics.parse_events(text):
            if parsed.recurrence_id == recurrence:
                return _event_of(source, locator, text, parsed)
        return self._occurrence(source, binding, event_id, recurrence)

    def _occurrence(
        self, source: str, binding: OwlBinding, event_id: str, recurrence: str
    ) -> CalendarEvent:
        """An occurrence the connector handed out: found by a one-second query at its start."""
        try:
            moment = datetime.strptime(recurrence, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            moment = datetime.strptime(recurrence, "%Y%m%d").replace(tzinfo=UTC)
        result = self._query(binding, moment, moment + timedelta(seconds=1), 50)
        for item in result.items:
            text = item.icalendar
            if item.id != event_id:
                continue
            event = _event(source, item, text)
            if event.locator == ics.instance_locator(event_id, recurrence):
                return event
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That occurrence is not there.")

    # --- writing --------------------------------------------------------------

    def resolve(self, source: str, locator: str) -> Current:
        uid, occurrence = ics.split_locator(locator)
        if occurrence:
            raise _no_series()
        item = self._item(self._binding(source), uid)
        if item is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        text = item.icalendar
        master = ics.master_of(text)
        if item.id != uid or master.uid != uid:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The event identity changed.")
        if master.recurring:
            raise _no_series()
        return Current(locator, ics.text_etag(text), raw=item)

    def validate(self, intent: CalendarIntent, current: Current | None) -> None:
        if isinstance(intent, CalendarRespond):
            raise ics.no_invitations()
        wanted = intent.event if isinstance(intent, CalendarCreate | CalendarUpdate) else None
        previous = None
        if current is not None:
            item = _stored(current)
            master = ics.master_of(item.icalendar)
            if not _own_appointment(item, master):
                previous = master
        ics.refuse_invitations(previous, wanted)
        if wanted and wanted.recurrence is not None:
            raise _no_series()

    def encode(self, intent: CalendarIntent, current: Current | None) -> Json:
        calendar = self._binding(intent.source).calendar_id
        now = self._clock.now()
        if isinstance(intent, CalendarCreate):
            assert intent.uid is not None
            args = wire.CalendarWriteArgs(
                calendar_id=calendar,
                event_id=intent.uid,
                kind="create",
                icalendar=ics.build_new(intent.uid, intent.event, now),
            )
        else:
            assert current is not None
            item = _stored(current)
            if isinstance(intent, CalendarUpdate):
                fields = intent.event
                if (
                    fields.visibility is None
                    and ics.master_of(item.icalendar).fields.visibility == "private"
                ):
                    fields = fields.model_copy(update={"visibility": "private"})
                args = wire.CalendarWriteArgs(
                    calendar_id=calendar,
                    event_id=item.id,
                    kind="update",
                    expected_icalendar=item.icalendar,
                    icalendar=ics.apply_fields(item.icalendar, fields, now),
                )
            else:
                args = wire.CalendarWriteArgs(
                    calendar_id=calendar,
                    event_id=item.id,
                    kind="delete",
                    expected_icalendar=item.icalendar,
                )
        return args.model_dump(mode="json", exclude_none=True, exclude={"calendar_id"})

    def _wire_args(self, prepared: Prepared) -> wire.CalendarWriteArgs:
        return wire.CalendarWriteArgs.model_validate(
            {**payload(prepared), "calendar_id": self._binding(prepared.source).calendar_id}
        )

    def preflight(self, prepared: Prepared) -> None:
        self._wire_args(prepared)

    def write(self, prepared: Prepared) -> WriteReply:
        args = self._wire_args(prepared)
        try:
            result = self._bridge.call(wire.Command.CALENDAR_WRITE, args, calendar=True).result
        except BridgeFailure as error:
            raise WriteFailure(
                str(error.code), sent=error.sent, mutation_possible=error.mutation != "none"
            ) from error
        return WriteReply(result)

    def observe(self, prepared: Prepared, reply: WriteReply | None) -> Observation:
        args = self._wire_args(prepared)
        uid = args.event_id
        if reply is None:
            item = self._item(self._binding(prepared.source), uid)
        else:
            if not isinstance(reply.hint, wire.CalendarWriteResult):
                return Observation(present=False, complete=False)
            header = reply.hint.item
            item = header
            if args.kind == "create" and header:
                uid = header.id
        if item is None:
            deleted = args.kind == "delete"
            return Observation(
                present=False,
                delete_confirmed=deleted,
                receipt=ProviderReceipt(locator=uid, verification=_CLIENT_ONLY)
                if deleted
                else None,
            )
        text = item.icalendar
        etag = ics.text_etag(text)
        matched = (
            args.icalendar is not None
            and _verified_text(item, uid, ics.master_of(args.icalendar).fields) is not None
        )
        return Observation(
            present=True,
            matches=matched,
            unchanged=args.expected_icalendar is not None
            and ics.text_etag(args.expected_icalendar) == etag,
            receipt=_receipt(uid, text) if matched else None,
        )

    # --- bridge ---------------------------------------------------------------

    def probe(self, source: str, token: str, *, cleanup_only: bool = False) -> bool:
        """Check the bound Owl calendar with one private appointment owned by this probe."""
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError("Invalid Owl freshness token")
        binding = self._binding(source)
        calendars = self._call(
            wire.CalendarListResult, wire.Command.CALENDAR_LIST, wire.StatusArgs()
        )
        calendar = next((c for c in calendars.calendars if c.id == binding.calendar_id), None)
        if calendar is None or calendar.type != "owl" or calendar.disabled or calendar.read_only:
            return False
        uid = f"chamberlain-freshness-{token}@chamberlain.local"
        title = f"Chamberlain calendar check {token}"
        if (
            cleanup_only
            and self._call(wire.StatusResult, wire.Command.STATUS, wire.StatusArgs()).write_busy
        ):
            return False
        start = datetime.fromtimestamp(int(token[:8], 16), UTC).replace(second=0)
        fields = EventFields(
            title=title,
            description=f"Automatic calendar check {token}",
            start=start,
            end=start + timedelta(minutes=1),
            visibility="private",
        )
        if cleanup_only:
            # Exchange may replace the submitted UID. Recover only our uniquely marked event.
            window = self._query(binding, start, fields.end, 250)
            if not window.complete or window.gaps:
                return False
            matches = [
                item for item in window.items if title == ics.master_of(item.icalendar).fields.title
            ]
            if not matches:
                return True
            if len(matches) != 1:
                return False
            actual_uid = matches[0].id
        else:
            if self._item(binding, uid) is not None:
                return False
            created = self._call(
                wire.CalendarWriteResult,
                wire.Command.CALENDAR_WRITE,
                wire.CalendarWriteArgs(
                    calendar_id=binding.calendar_id,
                    event_id=uid,
                    kind="create",
                    icalendar=ics.build_new(uid, fields, self._clock.now()),
                ),
            )
            item = created.item
            if item is None:
                return False
            actual_uid = item.id
        existing = self._item(binding, actual_uid)
        if existing is None or existing.id != actual_uid:
            return False
        text = existing.icalendar
        event = ics.master_of(text)
        if (
            event.uid != actual_uid
            or event.fields.title != title
            or event.fields.description != fields.description
            or event.fields.start != start
            or event.fields.end != fields.end
            or event.recurring
            or event.fields.attendees
        ):
            return False
        self._call(
            wire.CalendarWriteResult,
            wire.Command.CALENDAR_WRITE,
            wire.CalendarWriteArgs(
                calendar_id=binding.calendar_id,
                event_id=actual_uid,
                kind="delete",
                expected_icalendar=text,
            ),
        )
        removed = self._item(binding, actual_uid) is None
        return removed and (cleanup_only or event.fields.visibility == "private")

    def _binding(self, source: str) -> OwlBinding:
        binding = self._config.bindings.get(source)
        if binding is None:
            raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, "That source is not set up.")
        return binding

    @staticmethod
    def _compatible(hello: wire.Welcome) -> bool:
        return hello.v == wire.PROTOCOL_VERSION and hello.calendar_available

    def _call[M: Model](self, result: type[M], command: wire.Command, args: Model) -> M:
        return typed(self._bridge.call(command, args, calendar=True), result)

    def _query(
        self, binding: OwlBinding, start: datetime, end: datetime, limit: int
    ) -> wire.CalendarQueryResult:
        return self._call(
            wire.CalendarQueryResult,
            wire.Command.CALENDAR_QUERY,
            wire.CalendarQueryArgs(
                calendar_id=binding.calendar_id,
                start=start.isoformat(),
                end=end.isoformat(),
                limit=limit,
            ),
        )

    def _item(self, binding: OwlBinding, event_id: str) -> wire.CalendarItem | None:
        result = self._call(
            wire.CalendarReadResult,
            wire.Command.CALENDAR_READ,
            wire.CalendarReadArgs(
                calendar_id=binding.calendar_id,
                event_id=event_id,
            ),
        )
        return result.item


def _stored(current: Current) -> wire.CalendarItem:
    if not isinstance(current.raw, wire.CalendarItem):
        raise ValueError("Missing Owl calendar item")
    return current.raw


def _event(source: str, item: wire.CalendarItem, text: str) -> CalendarEvent:
    events = ics.parse_events(text)
    if not events:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE, "The calendar returned an item with no event."
        )
    parsed = events[0]
    recurrence = item.recurrence_id
    recurrence_id = recurrence if isinstance(recurrence, str) else parsed.recurrence_id
    return _event_of(source, ics.instance_locator(item.id, recurrence_id), text, parsed)


def _no_series() -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.CAPABILITY_UNSUPPORTED,
        "This calendar cannot write repeating events.",
        detail="recurrence-not-supported",
    )


def _event_of(source: str, locator: str, text: str, parsed: ics.ParsedEvent) -> CalendarEvent:
    base, occurrence = ics.split_locator(locator)
    series = base if occurrence is not None or parsed.recurring else None
    fields = parsed.fields
    if occurrence is None and parsed.rule is not None:
        fields = fields.model_copy(update={"recurrence": parsed.rule})
    return CalendarEvent(
        series_locator=series,
        source=source,
        locator=locator,
        uid=parsed.uid,
        etag=ics.text_etag(text),
        status=parsed.status,
        fields=fields,
        updated=parsed.updated,
    )


def _own_appointment(item: wire.CalendarItem, parsed: ics.ParsedEvent) -> bool:
    return (
        item.own_appointment is True
        and parsed.organiser
        and not parsed.fields.attendees
        and parsed.fields.visibility == "private"
    )


def _verified_text(item: wire.CalendarItem, event_id: str, wanted: EventFields) -> str | None:
    text = item.icalendar
    if item.id != event_id or item.recurrence_id:
        return None
    actual = ics.master_of(text)
    if (
        actual.uid != event_id
        or actual.recurring
        or actual.status.value == "cancelled"
        or actual.fields.attendees
        or (actual.organiser and not _own_appointment(item, actual))
        or not same_content(actual.fields, wanted)
    ):
        return None
    return text


def _receipt(event_id: str, text: str) -> ProviderReceipt:
    return ProviderReceipt(locator=event_id, etag=ics.text_etag(text), verification=_CLIENT_ONLY)
