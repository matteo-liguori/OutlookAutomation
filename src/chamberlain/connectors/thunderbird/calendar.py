"""CalendarPort for the Microsoft 365 calendar through Thunderbird with Owl.

Contract: docs/contracts/08-ports.md and docs/contracts/12-bridge-v2.md (calendar commands
are an optional capability). The connector talks to a `CalendarBridge`, a narrow protocol
that the Thunderbird client implements, so this module imports nothing from the bridge.
Thunderbird keeps no version of an event, so the etag is a hash of its iCalendar text.
The capability set is narrow: no invitations, no recurring-occurrence writes, and absence
is never proven (the client calendar is a cache, so `server_verified` is always False).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, cast

from pydantic import Field, JsonValue

from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.calendar_support.fields import same_content
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
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.base import Model, SourceId
from chamberlain.schema.errors import ChamberlainError, ErrorCode

PROTOCOL_VERSION: Final = 2
CAPABILITY: Final = "calendar"
_CLIENT_ONLY = Verification(client_verified=True, server_verified=False)
_SCAN_CAP = 5000

Json = dict[str, JsonValue]


@dataclass(frozen=True)
class BridgeHello:
    version: int
    capabilities: frozenset[str]


class CalendarBridgeFailure(Exception):
    """A bridge call failed. `sent` and `mutation` decide whether a write may have happened."""

    def __init__(self, code: str, message: str = "", *, sent: bool, mutation: str = "none") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.sent = sent
        self.mutation = mutation

    @property
    def proven_not_applied(self) -> bool:
        """The only grounds for `not_applied` (bridge contract section 7)."""
        return not self.sent or self.mutation == "none"


class CalendarBridge(Protocol):
    def hello(self) -> BridgeHello:
        """Connect and handshake. Raises `CalendarBridgeFailure` if the bridge is not there."""
        ...

    def request(self, cmd: str, args: Mapping[str, JsonValue], *, write: bool = False) -> Json:
        """One command. Raises `CalendarBridgeFailure`; never retries."""
        ...


class OwlBinding(Model):
    calendar_id: str = Field(min_length=1)
    """The Thunderbird calendar id."""


class OwlCalendarConfig(Model):
    bindings: dict[SourceId, OwlBinding]


class ThunderbirdCalendar:
    """Implements `chamberlain.ports.CalendarPort` over the Thunderbird calendar bridge."""

    def __init__(self, config: OwlCalendarConfig, bridge: CalendarBridge, clock: Clock) -> None:
        self._config = config
        self._bridge = bridge
        self._clock = clock

    # --- description ----------------------------------------------------------

    def capabilities(self, source: str) -> CapabilitySet:
        self._binding(source)
        try:
            usable = self._compatible(self._bridge.hello())
        except CalendarBridgeFailure as failure:
            raise _unavailable(failure) from None
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
            listing = self._bridge.request("calendar_list", {})
        except CalendarBridgeFailure:
            return SourceHealth(source=source, state=HealthState.UNAVAILABLE, checked_at=now)
        mine = next(
            (c for c in _items(listing, "calendars") if c.get("id") == binding.calendar_id), None
        )
        if mine is None or mine.get("disabled"):
            return SourceHealth(source=source, state=HealthState.UNAVAILABLE, checked_at=now)
        fresh = mine.get("fresh_as_of")
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
        offset = _decode_cursor(cursor)
        wanted = min(offset + query.limit + 1, _SCAN_CAP)
        result = self._read(
            "calendar_query",
            {
                "calendar_id": binding.calendar_id,
                "start": query.start.isoformat(),
                "end": query.end.isoformat(),
                "limit": wanted,
            },
        )
        found: list[CalendarEvent] = []
        unreadable = 0
        for item in _items(result, "items"):
            text = item.get("icalendar")
            if not isinstance(text, str):
                continue
            try:
                event = _event(source, item, text)
            except ChamberlainError:
                unreadable += 1
                continue
            if event.status.value == "cancelled" and not query.include_cancelled:
                continue
            fields = event.fields
            if fields.end < query.start or fields.start > query.end:
                continue
            if (
                query.text
                and query.text.casefold() not in f"{fields.title}\n{fields.description}".casefold()
            ):
                continue
            found.append(event)
        found.sort(key=lambda e: (e.fields.start, e.locator))
        window = found[offset : offset + query.limit]
        more = offset + query.limit < len(found)
        gaps = [Gap(source=source, reason=str(g)) for g in _list(result.get("gaps"))]
        if unreadable:
            gaps.append(Gap(source=source, reason="Some calendar items could not be read."))
        returned = len(_items(result, "items"))
        # `complete: false` at exactly the requested limit only means "there is more" (paging);
        # it is a coverage gap when the add-on stopped short of the limit or the limit is the cap.
        paging = returned >= wanted and wanted < _SCAN_CAP
        if result.get("complete") is not True and not gaps and not paging:
            gaps.append(Gap(source=source, reason="The calendar did not report a complete scan."))
        if returned >= wanted and wanted >= _SCAN_CAP:
            gaps.append(Gap(source=source, reason="The scan stopped at its limit."))
        return Page[CalendarEvent](
            items=tuple(window),
            cursor=f"o1.{offset + query.limit}" if more else None,
            complete=not more and not gaps,
            gaps=tuple(gaps[:1]),
        )

    def read(self, source: str, locator: str) -> CalendarEvent:
        binding = self._binding(source)
        event_id, recurrence = ics.split_locator(locator)
        item = self._item(binding, event_id)
        if item is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        text = str(item["icalendar"])
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
        result = self._read(
            "calendar_query",
            {
                "calendar_id": binding.calendar_id,
                "start": moment.isoformat(),
                "end": (moment + timedelta(seconds=1)).isoformat(),
                "limit": 50,
            },
        )
        for item in _items(result, "items"):
            text = item.get("icalendar")
            if item.get("id") != event_id or not isinstance(text, str):
                continue
            event = _event(source, item, text)
            if event.locator == ics.instance_locator(event_id, recurrence):
                return event
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That occurrence is not there.")

    # --- writing --------------------------------------------------------------

    def prepare(self, intent: CalendarIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only (P-9)."""
        binding = self._binding(intent.source)
        now = self._clock.now()
        data: Json
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
                    "event_id": intent.uid,
                    "icalendar": ics.build_new(intent.uid, intent.event, now),
                    "fields": intent.event.model_dump(mode="json"),
                }
            case CalendarUpdate():
                event_id, text = self._current(
                    binding, refs, intent.event_ref, intent.expected_etag, intent.event
                )
                data = {
                    "event_id": event_id,
                    "expected_icalendar": text,
                    "expected_etag": intent.expected_etag,
                    "icalendar": ics.apply_fields(text, intent.event, now),
                    "fields": intent.event.model_dump(mode="json"),
                }
            case CalendarDelete():
                event_id, text = self._current(
                    binding, refs, intent.event_ref, intent.expected_etag, None
                )
                data = {
                    "event_id": event_id,
                    "expected_icalendar": text,
                    "expected_etag": intent.expected_etag,
                }
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
            command, args = _write_command(binding, prepared)
        except ChamberlainError as error:
            return DispatchResult(outcome=DispatchOutcome.NOT_APPLIED, detail=str(error.code))
        try:
            result = self._bridge.request(command, args, write=True)
        except CalendarBridgeFailure as failure:
            outcome = (
                DispatchOutcome.NOT_APPLIED
                if failure.proven_not_applied
                else DispatchOutcome.UNKNOWN
            )
            return DispatchResult(outcome=outcome, detail=f"bridge {failure.code}")
        return self._verify(prepared, result)

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11). Absence is never proven: the client calendar is a cache (P-12)."""
        try:
            return self._reconcile(self._binding(prepared.source), prepared)
        except (ChamberlainError, CalendarBridgeFailure) as error:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail=str(error))

    # --- dispatch and reconcile -----------------------------------------------

    def _verify(self, prepared: Prepared, result: Json) -> DispatchResult:
        kind = prepared.intent_kind
        event_id = _text(prepared.data, "event_id")
        item = result.get("item")
        if kind is IntentKind.CALENDAR_DELETE:
            if item is None:
                receipt = ProviderReceipt(locator=event_id, verification=_CLIENT_ONLY)
                return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=receipt)
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="still present")
        wanted = EventFields.model_validate(prepared.data["fields"])
        text = item.get("icalendar") if isinstance(item, dict) else None
        if not isinstance(text, str):
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="no read-back")
        try:
            matches = same_content(ics.master_of(text).fields, wanted)
        except ChamberlainError:
            matches = False
        if not matches:
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail="read-back differs from the request"
            )
        return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=_receipt(event_id, text))

    def _reconcile(self, binding: OwlBinding, prepared: Prepared) -> ReconcileResult:
        kind = prepared.intent_kind
        data = prepared.data
        event_id = _text(data, "event_id")
        item = self._item(binding, event_id)
        text = str(item["icalendar"]) if item is not None else None
        if kind is IntentKind.CALENDAR_CREATE:
            if text is None:
                return ReconcileResult(status=ReconcileStatus.ABSENT, detail="not seen")
            return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=_receipt(event_id, text))
        if kind is IntentKind.CALENDAR_DELETE:
            if text is None:
                receipt = ProviderReceipt(locator=event_id, verification=_CLIENT_ONLY)
                return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=receipt)
            if ics.text_etag(text) == _text(data, "expected_etag"):
                return ReconcileResult(status=ReconcileStatus.ABSENT, detail="unchanged")
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="changed otherwise")
        if text is None:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="event not found")
        if ics.text_etag(text) == _text(data, "expected_etag"):
            return ReconcileResult(status=ReconcileStatus.ABSENT, detail="unchanged")
        wanted = EventFields.model_validate(data["fields"])
        if same_content(ics.master_of(text).fields, wanted):
            return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=_receipt(event_id, text))
        return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="changed otherwise")

    # --- bridge ---------------------------------------------------------------

    def _binding(self, source: str) -> OwlBinding:
        binding = self._config.bindings.get(source)
        if binding is None:
            raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, "That source is not set up.")
        return binding

    @staticmethod
    def _compatible(hello: BridgeHello) -> bool:
        return hello.version == PROTOCOL_VERSION and CAPABILITY in hello.capabilities

    def _read(self, command: str, args: Mapping[str, JsonValue]) -> Json:
        """A read; failures become `source.*` errors (P-17, P-18)."""
        try:
            return self._bridge.request(command, args)
        except CalendarBridgeFailure as failure:
            raise _unavailable(failure) from None

    def _item(self, binding: OwlBinding, event_id: str) -> Json | None:
        result = self._read(
            "calendar_read", {"calendar_id": binding.calendar_id, "event_id": event_id}
        )
        item = result.get("item")
        return item if isinstance(item, dict) else None

    def _current(
        self,
        binding: OwlBinding,
        refs: Mapping[str, str],
        ref: str,
        expected_etag: str,
        wanted: EventFields | None,
    ) -> tuple[str, str]:
        locator = refs.get(ref)
        if locator is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not known.")
        event_id, recurrence = ics.split_locator(locator)
        if recurrence is not None:
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "A single occurrence of a recurring event cannot be written.",
            )
        item = self._item(binding, event_id)
        if item is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not there.")
        text = str(item["icalendar"])
        if ics.text_etag(text) != expected_etag:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The event changed since it was read.")
        ics.refuse_invitations(ics.master_of(text), wanted)
        return event_id, text


def _write_command(binding: OwlBinding, prepared: Prepared) -> tuple[str, Json]:
    data = prepared.data
    base: Json = {"calendar_id": binding.calendar_id, "event_id": _text(data, "event_id")}
    match prepared.intent_kind:
        case IntentKind.CALENDAR_CREATE:
            return "calendar_create", {**base, "icalendar": _text(data, "icalendar")}
        case IntentKind.CALENDAR_UPDATE:
            return "calendar_update", {
                **base,
                "expected_icalendar": _text(data, "expected_icalendar"),
                "icalendar": _text(data, "icalendar"),
            }
        case _:
            return "calendar_delete", {
                **base,
                "expected_icalendar": _text(data, "expected_icalendar"),
            }


def _event(source: str, item: Json, text: str) -> CalendarEvent:
    events = ics.parse_events(text)
    if not events:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE, "The calendar returned an item with no event."
        )
    parsed = events[0]
    recurrence = item.get("recurrence_id")
    recurrence_id = recurrence if isinstance(recurrence, str) else parsed.recurrence_id
    return _event_of(source, ics.instance_locator(str(item["id"]), recurrence_id), text, parsed)


def _event_of(source: str, locator: str, text: str, parsed: ics.ParsedEvent) -> CalendarEvent:
    return CalendarEvent(
        source=source,
        locator=locator,
        uid=parsed.uid,
        etag=ics.text_etag(text),
        status=parsed.status,
        fields=parsed.fields,
        updated=parsed.updated,
    )


def _receipt(event_id: str, text: str) -> ProviderReceipt:
    return ProviderReceipt(locator=event_id, etag=ics.text_etag(text), verification=_CLIENT_ONLY)


def _items(result: Json, key: str) -> list[Json]:
    return [i for i in _list(result.get(key)) if isinstance(i, dict)]


def _list(value: object) -> list[JsonValue]:
    return cast(list[JsonValue], value) if isinstance(value, list) else []


def _text(data: Mapping[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "A prepared field is missing.")
    return value


def _unavailable(failure: CalendarBridgeFailure) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.SOURCE_UNAVAILABLE,
        "Thunderbird did not answer for the calendar.",
        fix="Install the current Chamberlain Thunderbird add-on and restart Thunderbird.",
        detail=str(failure),
    )


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix, _, number = cursor.partition(".")
    if prefix != "o1" or not number.isdigit():
        raise ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid.")
    return int(number)
