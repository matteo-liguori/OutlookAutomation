"""In-memory CalendarPort, shaped like Google Calendar.

Events have an `id` (the locator), an iCalendar `uid`, an `etag` that changes
on every write, a `status` and an `updated` time. Deleting marks an event
cancelled rather than removing it.

Contract: docs/contracts/08-ports.md (P-16).
"""

from collections.abc import Mapping

from pydantic import JsonValue

from chamberlain.connectors.fake.world import DispatchRecord, FakeWorld, Fault, StoredEvent
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery, EventFields, EventStatus
from chamberlain.domain.capabilities import (
    Capability,
    CapabilityName,
    CapabilitySet,
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
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_K = IntentKind
_VERIFIED = Verification(client_verified=True, server_verified=True)
_PROVABLE = frozenset(
    {_K.CALENDAR_CREATE.value, _K.CALENDAR_UPDATE.value, _K.CALENDAR_DELETE.value}
)


def _text(data: Mapping[str, JsonValue], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"prepared field {key} is not text")
    return value


class FakeCalendar:
    """Implements `chamberlain.ports.CalendarPort` over a `FakeWorld`."""

    def __init__(self, world: FakeWorld) -> None:
        self._world = world

    # --- seeding (tests only) -------------------------------------------------

    def add_source(self, source: str) -> None:
        self._world.events.setdefault(source, {})

    def add_event(self, source: str, fields: EventFields, *, uid: str | None = None) -> str:
        """Store an event and return its locator."""
        return self._create(source, fields, uid).id

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
        return CapabilitySet.model_validate(
            {
                "source": source,
                "capabilities": [
                    Capability(
                        name=CapabilityName.CALENDAR_READ, level=VerificationLevel.IMPLEMENTED
                    ),
                    Capability(
                        name=CapabilityName.CALENDAR_WRITE,
                        level=VerificationLevel.IMPLEMENTED,
                        absence_provable=_PROVABLE,
                    ),
                ],
            }
        )

    def health(self, source: str) -> SourceHealth:
        self._world.require_source(self._world.events, source)
        return self._world.health(source)

    # --- port: reading --------------------------------------------------------

    def events(
        self, source: str, query: CalendarQuery, cursor: str | None = None
    ) -> Page[CalendarEvent]:
        self._begin_read(source)
        try:
            offset = int(cursor or "0")
        except ValueError:
            raise ChamberlainError(
                ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid."
            ) from None
        matches = [e for e in self._world.events[source].values() if self._matches(e, query)]
        matches.sort(key=lambda e: (e.fields.start, e.id))
        window = matches[offset : offset + query.limit]
        more = offset + query.limit < len(matches)
        gaps: tuple[Gap, ...] = ()
        if self._world.take(Fault.STALE):
            gaps = (Gap.model_validate({"source": source, "reason": "The calendar is stale."}),)
        return Page[CalendarEvent](
            items=tuple(self._event(source, e) for e in window),
            cursor=str(offset + query.limit) if more else None,
            complete=not more and not gaps,
            gaps=gaps,
        )

    def read(self, source: str, locator: str) -> CalendarEvent:
        self._begin_read(source)
        return self._event(source, self._one(source, locator))

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
                token = intent.uid
                data = {"fields": intent.event.model_dump(mode="json")}
            case CalendarUpdate():
                stored = self._current(source, refs[intent.event_ref], intent.expected_etag)
                data = {
                    "locator": stored.id,
                    "expected_etag": intent.expected_etag,
                    "fields": intent.event.model_dump(mode="json"),
                }
            case CalendarDelete():
                stored = self._current(source, refs[intent.event_ref], intent.expected_etag)
                data = {"locator": stored.id, "expected_etag": intent.expected_etag}
        return Prepared.model_validate(
            {"source": source, "intent_kind": intent.kind, "reconcile_token": token, "data": data}
        )

    def dispatch(self, prepared: Prepared, key: str) -> DispatchResult:
        """The one mutation (P-10). Never retries and never raises."""
        world = self._world
        world.begin_call()
        kind = prepared.intent_kind

        def apply() -> ProviderReceipt:
            return self._apply(prepared)

        def late() -> None:
            try:
                apply()
            except ChamberlainError:
                return

        if world.take(Fault.BUSY_BEFORE_APPLY):
            world.dispatch_log.append(DispatchRecord(key, kind, applied=False))
            return DispatchResult(outcome=DispatchOutcome.NOT_APPLIED, detail="fake: busy")
        if world.take(Fault.LOSE_BEFORE_APPLY):
            world.dispatch_log.append(DispatchRecord(key, kind, applied=False))
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="fake: lost before apply")
        if world.take(Fault.SLOW):
            world.dispatch_log.append(DispatchRecord(key, kind, applied=False))
            world.apply_late(late)
            world.clock.advance(seconds=world.deadline_seconds + 1)
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="fake: deadline passed")
        try:
            receipt = apply()
        except ChamberlainError as error:
            world.dispatch_log.append(DispatchRecord(key, kind, applied=False))
            return DispatchResult(
                outcome=DispatchOutcome.NOT_APPLIED, detail=f"fake: precondition {error.code}"
            )
        world.dispatch_log.append(DispatchRecord(key, kind, applied=True))
        if world.take(Fault.LOSE_RESPONSE_AFTER_APPLY):
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="fake: response lost")
        return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=receipt)

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11). Settles only from an exact read-back."""
        source = prepared.source
        world = self._world
        world.begin_call()
        if world.take(Fault.SLOW):
            world.clock.advance(seconds=world.deadline_seconds + 1)
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="fake: deadline passed")
        if world.take(Fault.STALE):
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="fake: stale calendar")
        data = prepared.data
        absent = ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)
        if prepared.intent_kind is _K.CALENDAR_CREATE:
            found = [e for e in world.events[source].values() if e.uid == prepared.reconcile_token]
            return self._present(found[0]) if found else absent
        stored = world.events[source].get(_text(data, "locator"))
        if stored is None:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="fake: event missing")
        unchanged = stored.etag == _text(data, "expected_etag")
        if prepared.intent_kind is _K.CALENDAR_DELETE:
            if stored.status is EventStatus.CANCELLED:
                return self._present(stored)
            return absent if unchanged else ReconcileResult(status=ReconcileStatus.UNKNOWN)
        if unchanged:
            return absent
        wanted = EventFields.model_validate(data["fields"])
        if stored.fields == wanted and stored.status is not EventStatus.CANCELLED:
            return self._present(stored)
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
            return ProviderReceipt(locator=stored.id, etag=stored.etag, verification=_VERIFIED)
        stored = self._current(source, _text(data, "locator"), _text(data, "expected_etag"))
        if prepared.intent_kind is _K.CALENDAR_DELETE:
            stored.status = EventStatus.CANCELLED
        else:
            stored.fields = EventFields.model_validate(data["fields"])
        stored.sequence += 1
        stored.updated = self._world.clock.now()
        return ProviderReceipt(locator=stored.id, etag=stored.etag, verification=_VERIFIED)

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

    def _present(self, stored: StoredEvent) -> ReconcileResult:
        receipt = ProviderReceipt(locator=stored.id, etag=stored.etag, verification=_VERIFIED)
        return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=receipt)

    @staticmethod
    def _event(source: str, stored: StoredEvent) -> CalendarEvent:
        return CalendarEvent.model_validate(
            {
                "source": source,
                "locator": stored.id,
                "uid": stored.uid,
                "etag": stored.etag,
                "status": stored.status,
                "fields": stored.fields,
                "updated": stored.updated,
            }
        )

    @staticmethod
    def _matches(stored: StoredEvent, query: CalendarQuery) -> bool:
        if stored.status is EventStatus.CANCELLED and not query.include_cancelled:
            return False
        if stored.fields.end < query.start or stored.fields.start > query.end:
            return False
        if query.text:
            haystack = f"{stored.fields.title}\n{stored.fields.description}".casefold()
            return query.text.casefold() in haystack
        return True
