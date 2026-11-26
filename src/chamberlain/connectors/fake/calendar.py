"""In-memory CalendarPort, shaped like Google Calendar.

Events have an `id` (the locator), an iCalendar `uid`, an `etag` that changes
on every write, a `status` and an `updated` time. Deleting marks an event
cancelled rather than removing it.

Contract: docs/contracts/08-ports.md (P-16).
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import JsonValue

from chamberlain.connectors.calendar_support.ics import split_locator
from chamberlain.connectors.calendar_support.paging import matches_window
from chamberlain.connectors.calendar_support.port import calendar_capabilities
from chamberlain.connectors.fake.world import FakeWorld, StoredEvent, text
from chamberlain.domain.calendar import (
    CalendarEvent,
    CalendarQuery,
    EventFields,
    EventStatus,
    Participant,
    Response,
)
from chamberlain.domain.capabilities import (
    CapabilitySet,
    SourceHealth,
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
    DispatchResult,
    Prepared,
    ProviderReceipt,
    ReconcileResult,
    ReconcileStatus,
    Verification,
)
from chamberlain.domain.recurrence import expand, occurrence_id, parse_occurrence_id
from chamberlain.ports.page import Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_K = IntentKind
_VERIFIED = Verification(client_verified=True, server_verified=True)
_ANSWERS = {
    "accepted": Response.ACCEPTED,
    "tentative": Response.TENTATIVE,
    "declined": Response.DECLINED,
}


class FakeCalendar:
    """Implements `chamberlain.ports.CalendarPort` over a `FakeWorld`."""

    def __init__(self, world: FakeWorld) -> None:
        self._world = world

    # --- seeding (tests only) -------------------------------------------------

    def add_source(
        self,
        source: str,
        address: str | None = None,
        *,
        invitations: bool = True,
        recurrence: bool = True,
    ) -> None:
        """Add a calendar. `address` is its owner's: invitations are delivered by address.

        With `invitations=False` the calendar declines to send or answer invitations, as an
        Owl calendar does. With `recurrence=False` it reads the occurrences of a series but
        declines to write one, as an Owl calendar does.
        """
        world = self._world
        world.events.setdefault(source, {})
        world.addresses.setdefault(source, f"{source.split(':')[-1]}@fake.example")
        world.invitations[source] = invitations
        world.recurrence[source] = recurrence
        if address is not None:
            world.addresses[source] = address.lower()

    def add_event(
        self,
        source: str,
        fields: EventFields,
        *,
        uid: str | None = None,
        organiser: str | None = None,
        my_response: Response | None = None,
    ) -> str:
        """Store an event and return its locator.

        With `organiser` and `my_response` the event is an invitation someone outside the fake
        world sent: answering it is recorded in `responses_sent`.
        """
        stored = self._create(source, fields, uid)
        stored.organiser = organiser
        stored.my_response = my_response
        if organiser is not None:
            stored.participants = dict.fromkeys(fields.attendees, Response.NEEDS_ACTION)
        return stored.id

    def undelivered(self, source: str) -> list[tuple[str, str]]:
        """`(uid, address)` of invitees the organiser's calendar could not reach in this world."""
        return list(self._world.undelivered.get(source, []))

    def responses_sent(self, source: str) -> list[tuple[str, str, Response]]:
        """Answers sent to organisers outside the fake world."""
        return list(self._world.responses_sent.get(source, []))

    def copy_for(self, source: str, uid: str) -> StoredEvent | None:
        """The latest copy of an event on one calendar, cancelled or not, for assertions."""
        found = [e for e in self._world.events[source].values() if e.uid == uid]
        return found[-1] if found else None

    def count(self, source: str, uid: str) -> int:
        """How many live (not cancelled) events carry this uid."""
        return len(
            [
                e
                for e in self._world.events[source].values()
                if e.uid == uid and e.status is not EventStatus.CANCELLED
            ]
        )

    # --- port: description ----------------------------------------------------

    def capabilities(self, source: str) -> CapabilitySet:
        self._world.require_source(self._world.events, source)
        return calendar_capabilities(
            source,
            server=True,
            invitations=self._world.invitations.get(source, True),
            recurrence=self._world.recurrence.get(source, True),
        )

    def health(self, source: str) -> SourceHealth:
        self._world.require_source(self._world.events, source)
        return self._world.health(source)

    # --- port: reading --------------------------------------------------------

    def events(
        self, source: str, query: CalendarQuery, cursor: str | None = None
    ) -> Page[CalendarEvent]:
        self._begin_read(source)
        return self._world.page(
            Page[CalendarEvent],
            source,
            self._window(source, query),
            cursor,
            query.limit,
            "calendar",
        )

    def read(self, source: str, locator: str) -> CalendarEvent:
        self._begin_read(source)
        base, occurrence = split_locator(locator)
        stored = self._one(source, base)
        if occurrence is None:
            return self._event(source, stored)
        slot = self._slot(stored, occurrence)
        if slot is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE, "That occurrence is not there."
            )
        return self._occurrence(source, stored, *slot)

    # --- port: writing --------------------------------------------------------

    def prepare(self, intent: CalendarIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only (P-9)."""
        source = intent.source
        self._begin_read(source)
        data: dict[str, JsonValue]
        token: str | None = None
        match intent:
            case CalendarCreate():
                if intent.uid is None:
                    raise ChamberlainError(
                        ErrorCode.REQUEST_INVALID, "The event has no uid.", field="uid"
                    )
                self._check_scope(source, None, "", intent.event)
                token = intent.uid
                data = {"fields": intent.event.model_dump(mode="json")}
            case CalendarUpdate():
                locator = refs[intent.event_ref]
                self._check_scope(source, intent.scope, locator, intent.event)
                stored = self._current(source, split_locator(locator)[0], intent.expected_etag)
                self._own_event(stored)
                data = {
                    "locator": locator,
                    "expected_etag": intent.expected_etag,
                    "fields": intent.event.model_dump(mode="json"),
                }
            case CalendarDelete():
                locator = refs[intent.event_ref]
                self._check_scope(source, intent.scope, locator, None)
                stored = self._current(source, split_locator(locator)[0], intent.expected_etag)
                self._own_event(stored)
                data = {"locator": locator, "expected_etag": intent.expected_etag}
            case CalendarRespond():
                stored = self._current(source, refs[intent.event_ref], intent.expected_etag)
                if stored.my_response is None or stored.organiser is None:
                    raise ChamberlainError(
                        ErrorCode.REQUEST_INVALID, "That event is not an invitation."
                    )
                data = {
                    "locator": stored.id,
                    "expected_etag": intent.expected_etag,
                    "response": intent.response,
                }
        return Prepared.model_validate(
            {"source": source, "intent_kind": intent.kind, "reconcile_token": token, "data": data}
        )

    def dispatch(self, prepared: Prepared, key: str) -> DispatchResult:
        """The one mutation (P-10). Never retries and never raises."""
        return self._world.run_dispatch(prepared, key, lambda: self._apply(prepared))

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11). Settles only from an exact read-back."""
        source = prepared.source
        world = self._world
        if (gated := world.reconcile_gate("calendar")) is not None:
            return gated
        data = prepared.data
        absent = ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)
        if prepared.intent_kind is _K.CALENDAR_CREATE:
            found = [e for e in world.events[source].values() if e.uid == prepared.reconcile_token]
            return self._present(found[0]) if found else absent
        locator = text(data, "locator")
        base, occurrence = split_locator(locator)
        stored = world.events[source].get(base)
        if stored is None:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="fake: event missing")
        unchanged = stored.etag == text(data, "expected_etag")
        if prepared.intent_kind is _K.CALENDAR_RESPOND:
            if unchanged:
                return absent
            if stored.my_response is _ANSWERS[text(data, "response")]:
                return self._present(stored)
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="fake: changed otherwise")
        if prepared.intent_kind is _K.CALENDAR_DELETE:
            gone = (
                occurrence in stored.overrides and stored.overrides[occurrence] is None
                if occurrence is not None
                else stored.status is EventStatus.CANCELLED
            )
            if gone:
                return self._present(stored, locator)
            return absent if unchanged else ReconcileResult(status=ReconcileStatus.UNKNOWN)
        if unchanged:
            return absent
        wanted = EventFields.model_validate(data["fields"])
        if occurrence is not None:
            same = stored.overrides.get(occurrence) == _occurrence_fields(
                stored, occurrence, wanted
            )
        else:
            same = stored.fields == _series_fields(stored, wanted)
        if same and stored.status is not EventStatus.CANCELLED:
            return self._present(stored, locator)
        return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="fake: changed otherwise")

    # --- helpers --------------------------------------------------------------

    def _apply(self, prepared: Prepared) -> ProviderReceipt:
        source = prepared.source
        data = prepared.data
        if prepared.intent_kind is _K.CALENDAR_CREATE:
            uid = prepared.reconcile_token or ""
            if any(e.uid == uid for e in self._world.events[source].values()):
                raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "An event with this uid exists.")
            stored = self._create(source, EventFields.model_validate(data["fields"]), uid)
            self._invite(source, stored)
            return ProviderReceipt(locator=stored.id, etag=stored.etag, verification=_VERIFIED)
        locator = text(data, "locator")
        base, occurrence = split_locator(locator)
        stored = self._current(source, base, text(data, "expected_etag"))
        if prepared.intent_kind is _K.CALENDAR_RESPOND:
            self._answer(source, stored, _ANSWERS[text(data, "response")])
        elif occurrence is not None:
            if self._slot(stored, occurrence) is None:
                raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "That occurrence is gone.")
            if prepared.intent_kind is _K.CALENDAR_DELETE:
                stored.overrides[occurrence] = None
            else:
                wanted = EventFields.model_validate(data["fields"])
                stored.overrides[occurrence] = _occurrence_fields(stored, occurrence, wanted)
        elif prepared.intent_kind is _K.CALENDAR_DELETE:
            stored.status = EventStatus.CANCELLED
            self._cancel_copies(source, stored, stored.participants)
        else:
            before = stored.fields
            stored.fields = _series_fields(stored, EventFields.model_validate(data["fields"]))
            if before.recurrence is None:
                self._reinvite(source, stored, before)
        stored.sequence += 1
        stored.updated = self._world.clock.now()
        return ProviderReceipt(locator=locator, etag=stored.etag, verification=_VERIFIED)

    def _create(self, source: str, fields: EventFields, uid: str | None) -> StoredEvent:
        event_id = self._world.next_event_id()
        stored = StoredEvent(
            id=event_id,
            uid=uid or f"{event_id}@fake.example",
            sequence=1,
            status=EventStatus.CONFIRMED,
            fields=fields,
            updated=self._world.clock.now(),
        )
        self._world.events[source][event_id] = stored
        return stored

    # --- invitations ----------------------------------------------------------

    @staticmethod
    def _own_event(stored: StoredEvent) -> None:
        """Only the organiser changes or cancels an invitation (the answer is `respond`)."""
        if stored.my_response is not None:
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "Only the organiser can change that event.",
                detail="invitee-copy",
            )

    def _owner_source(self, address: str, *, not_source: str) -> str | None:
        for source, owner in self._world.addresses.items():
            if owner == address and source != not_source and source in self._world.events:
                return source
        return None

    def _invite(self, source: str, stored: StoredEvent) -> None:
        """Deliver the invitation to every attendee whose calendar is in this world."""
        world = self._world
        organiser = world.addresses[source]
        stored.participants = {
            a: Response.NEEDS_ACTION for a in stored.fields.attendees if a != organiser
        }
        for address in stored.participants:
            self._deliver(source, stored, address, organiser)

    def _deliver(self, source: str, stored: StoredEvent, address: str, organiser: str) -> None:
        target = self._owner_source(address, not_source=source)
        if target is None:
            self._world.undelivered.setdefault(source, []).append((stored.uid, address))
            return
        guests = tuple(a for a in (organiser, *stored.fields.attendees) if a != address)
        copy = self._create(
            target, stored.fields.model_copy(update={"attendees": guests}), stored.uid
        )
        copy.organiser = organiser
        copy.my_response = Response.NEEDS_ACTION
        copy.participants = dict(stored.participants)
        copy.invite_of = (source, stored.id)

    def _copies(self, source: str, stored: StoredEvent) -> list[tuple[str, StoredEvent]]:
        """The invitees' copies of an organiser's event, wherever they are held."""
        return [
            (other, event)
            for other, events in self._world.events.items()
            for event in events.values()
            if event.invite_of == (source, stored.id)
        ]

    def _cancel_copies(self, source: str, stored: StoredEvent, who: Mapping[str, Response]) -> None:
        for other, copy in self._copies(source, stored):
            if self._world.addresses[other] in who:
                copy.status = EventStatus.CANCELLED
                copy.sequence += 1
                copy.updated = self._world.clock.now()

    def _reinvite(self, source: str, stored: StoredEvent, before: EventFields) -> None:
        """Carry an update to the invitees: new ones are invited, removed ones are cancelled."""
        world = self._world
        organiser = world.addresses[source]
        wanted = [a for a in stored.fields.attendees if a != organiser]
        timing_changed = (before.start, before.end) != (stored.fields.start, stored.fields.end)
        kept = {
            a: (Response.NEEDS_ACTION if timing_changed else stored.participants.get(a))
            or Response.NEEDS_ACTION
            for a in wanted
        }
        gone = {a: r for a, r in stored.participants.items() if a not in kept}
        self._cancel_copies(source, stored, gone)
        stored.participants = kept
        delivered = {world.addresses[other] for other, _ in self._copies(source, stored)}
        for other, copy in self._copies(source, stored):
            address = world.addresses[other]
            if address not in kept:
                continue
            guests = tuple(a for a in (organiser, *stored.fields.attendees) if a != address)
            copy.fields = stored.fields.model_copy(update={"attendees": guests})
            copy.participants = dict(kept)
            if timing_changed:
                copy.my_response = Response.NEEDS_ACTION
            copy.status = EventStatus.CONFIRMED
            copy.sequence += 1
            copy.updated = world.clock.now()
        for address in kept:
            if address not in delivered:
                self._deliver(source, stored, address, organiser)

    def _answer(self, source: str, stored: StoredEvent, response: Response) -> None:
        """Record the owner's answer, and tell the organiser's calendar if it is in this world."""
        world = self._world
        stored.my_response = response
        me = world.addresses[source]
        if stored.invite_of is None:
            world.responses_sent.setdefault(source, []).append(
                (stored.uid, stored.organiser or "", response)
            )
            return
        organiser_source, event_id = stored.invite_of
        theirs = world.events[organiser_source].get(event_id)
        if theirs is None:
            return
        theirs.participants[me] = response
        for _, copy in self._copies(organiser_source, theirs):
            copy.participants = dict(theirs.participants)
        theirs.sequence += 1
        theirs.updated = world.clock.now()

    def _begin_read(self, source: str) -> None:
        self._world.require_source(self._world.events, source)
        self._world.begin_read(source)

    def _one(self, source: str, locator: str) -> StoredEvent:
        stored = self._world.events[source].get(locator)
        if stored is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        return stored

    def _current(self, source: str, locator: str, expected_etag: str) -> StoredEvent:
        stored = self._one(source, locator)
        if stored.etag != expected_etag or stored.status is EventStatus.CANCELLED:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The event changed since it was read.")
        return stored

    def _present(self, stored: StoredEvent, locator: str | None = None) -> ReconcileResult:
        receipt = ProviderReceipt(
            locator=locator or stored.id, etag=stored.etag, verification=_VERIFIED
        )
        return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=receipt)

    @staticmethod
    def _event(source: str, stored: StoredEvent) -> CalendarEvent:
        """A stored event as read: a series is its first occurrence plus the rule."""
        return CalendarEvent.model_validate(
            {
                "source": source,
                "locator": stored.id,
                "uid": stored.uid,
                "etag": stored.etag,
                "status": stored.status,
                "fields": stored.fields,
                "updated": stored.updated,
                "organiser": stored.organiser,
                "my_response": stored.my_response,
                "participants": tuple(
                    Participant(address=a, response=r) for a, r in stored.participants.items()
                ),
                "series_locator": stored.id if stored.fields.recurrence is not None else None,
            }
        )

    # --- repeating events -----------------------------------------------------

    def _check_scope(
        self, source: str, scope: str | None, locator: str, fields: EventFields | None
    ) -> None:
        """What the connector checks itself: it says what it cannot do, never fakes it (P-26)."""
        occurrence = split_locator(locator)[1]
        repeats = scope is not None or (fields is not None and fields.recurrence is not None)
        if repeats and not self._world.recurrence.get(source, True):
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "This calendar cannot write repeating events.",
                detail="recurrence-not-supported",
            )
        if scope is not None and (scope == "occurrence") != (occurrence is not None):
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "The scope does not fit that handle.", field="scope"
            )
        if scope == "occurrence" and fields is not None and fields.recurrence is not None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "One occurrence takes no repeat rule.", field="event"
            )

    @staticmethod
    def _slot(stored: StoredEvent, occurrence: str) -> tuple[datetime, datetime] | None:
        """The original start and end of the occurrence an id names, or None."""
        rule = stored.fields.recurrence
        parsed = parse_occurrence_id(occurrence)
        if rule is None or parsed is None:
            return None
        moment = parsed[0]
        for begin, finish in expand(
            rule,
            stored.fields.start,
            stored.fields.end,
            all_day=stored.fields.all_day,
            window_start=moment,
            window_end=moment,
        ):
            if begin == moment:
                return begin, finish
        return None

    @staticmethod
    def _occurrence(
        source: str, stored: StoredEvent, begin: datetime, finish: datetime
    ) -> CalendarEvent:
        """One occurrence as read: the series' fields, or what it was changed to."""
        name = occurrence_id(begin, all_day=stored.fields.all_day)
        changed = stored.overrides.get(name)
        cancelled = name in stored.overrides and changed is None
        fields = changed or stored.fields.model_copy(
            update={"start": begin, "end": finish, "recurrence": None}
        )
        status = EventStatus.CANCELLED if cancelled else stored.status
        return CalendarEvent.model_validate(
            {
                "source": source,
                "locator": f"{stored.id}#{name}",
                "uid": stored.uid,
                "etag": stored.etag,
                "status": status,
                "fields": fields,
                "updated": stored.updated,
                "series_locator": stored.id,
            }
        )

    def _window(self, source: str, query: CalendarQuery) -> list[CalendarEvent]:
        """Every event or occurrence overlapping the window, in time order."""
        found: list[CalendarEvent] = []
        pad = timedelta(days=30)  # a moved occurrence may start away from its original slot
        for stored in self._world.events[source].values():
            rule = stored.fields.recurrence
            if rule is None:
                event = self._event(source, stored)
                if matches_window(query, event.fields, event.status):
                    found.append(event)
                continue
            if stored.status is EventStatus.CANCELLED:
                continue
            for begin, finish in expand(
                rule,
                stored.fields.start,
                stored.fields.end,
                all_day=stored.fields.all_day,
                window_start=query.start - pad,
                window_end=query.end + pad,
            ):
                event = self._occurrence(source, stored, begin, finish)
                if matches_window(query, event.fields, event.status):
                    found.append(event)
        found.sort(key=lambda e: (e.fields.start, e.locator))
        return found


def _series_fields(stored: StoredEvent, wanted: EventFields) -> EventFields:
    """An update keeps recurrence and visibility when omitted (P-25)."""
    return wanted.model_copy(
        update={
            "recurrence": wanted.recurrence or stored.fields.recurrence,
            "visibility": wanted.visibility or stored.fields.visibility,
        }
    )


def _occurrence_fields(stored: StoredEvent, occurrence: str, wanted: EventFields) -> EventFields:
    before = stored.overrides.get(occurrence) or stored.fields
    return wanted.model_copy(
        update={"recurrence": None, "visibility": wanted.visibility or before.visibility}
    )
