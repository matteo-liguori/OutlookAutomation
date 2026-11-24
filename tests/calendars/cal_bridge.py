"""A fake Thunderbird calendar bridge: the commands of docs/contracts/12-bridge-v2.md (calendar)."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import JsonValue

from chamberlain.connectors.fake.world import Fault
from chamberlain.connectors.thunderbird.calendar import BridgeHello, CalendarBridgeFailure, Json
from chamberlain.ports.clock import ManualClock


class FakeCalendarBridge:
    def __init__(
        self,
        clock: ManualClock,
        calendar_id: str = "cal-1",
        *,
        version: int = 2,
        capabilities: frozenset[str] = frozenset({"calendar", "read"}),
    ) -> None:
        self.clock = clock
        self.calendar_id = calendar_id
        self.version = version
        self.capabilities = capabilities
        self.items: dict[str, str] = {}
        self.occurrences: list[tuple[str, str, str]] = []
        """`(event id, recurrence id, icalendar)` for series: the query answers with these."""
        self.requests: list[tuple[str, bool]] = []
        self.offline = False
        self.fresh_as_of: str | None = None
        self.deadline = 45.0
        self._armed: list[Fault] = []
        self._late: list[tuple[Any, Callable[[], None]]] = []

    # --- test controls --------------------------------------------------------

    def arm(self, fault: Fault) -> None:
        self._armed.append(fault)

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds=seconds)
        now = self.clock.now()
        due = [item for item in self._late if item[0] <= now]
        self._late = [item for item in self._late if item[0] > now]
        for _, apply in due:
            apply()

    # --- the bridge -----------------------------------------------------------

    def hello(self) -> BridgeHello:
        if self.offline:
            raise CalendarBridgeFailure("not_connected", sent=False)
        return BridgeHello(self.version, self.capabilities)

    def request(self, cmd: str, args: Mapping[str, JsonValue], *, write: bool = False) -> Json:
        self.requests.append((cmd, write))
        if self.offline:
            raise CalendarBridgeFailure("not_connected", sent=False)
        if args.get("calendar_id", self.calendar_id) != self.calendar_id:
            raise CalendarBridgeFailure("not_found", "no such calendar", sent=True)
        if cmd == "calendar_list":
            entry: Json = {"id": self.calendar_id, "name": "Calendar", "disabled": False}
            if self.fresh_as_of:
                entry["fresh_as_of"] = self.fresh_as_of
            return {"calendars": [entry]}
        if cmd == "calendar_query":
            return self._query(args)
        event_id = str(args["event_id"])
        if cmd == "calendar_read":
            return {"item": self._item(event_id)}
        return self._write(cmd, event_id, args)

    # --- internals ------------------------------------------------------------

    def _item(self, event_id: str) -> Json | None:
        text = self.items.get(event_id)
        return None if text is None else {"id": event_id, "recurrence_id": None, "icalendar": text}

    def _query(self, args: Mapping[str, JsonValue]) -> Json:
        """Like the add-on: `complete` is false when the limit or staleness cut the scan."""
        limit = int(str(args["limit"]))
        start = datetime.fromisoformat(str(args["start"]))
        end = datetime.fromisoformat(str(args["end"]))
        stale = Fault.STALE in self._armed
        if stale:
            self._armed.remove(Fault.STALE)
        everything: list[JsonValue] = [
            {"id": i, "recurrence_id": None, "icalendar": self.items[i]}
            for i in sorted(self.items)
            if i not in {o[0] for o in self.occurrences}
        ]
        for event_id, recurrence, text in self.occurrences:
            moment = datetime.strptime(recurrence, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            if start <= moment <= end:
                everything.append({"id": event_id, "recurrence_id": recurrence, "icalendar": text})
        return {
            "items": everything[:limit],
            "complete": not stale and len(everything) <= limit,
            "gaps": ["The calendar has not synchronised."] if stale else [],
        }

    def _write(self, cmd: str, event_id: str, args: Mapping[str, JsonValue]) -> Json:
        def apply() -> Json:
            if cmd == "calendar_create":
                if event_id in self.items:
                    raise CalendarBridgeFailure("changed", "exists", sent=True)
                self.items[event_id] = str(args["icalendar"])
            else:
                if self.items.get(event_id) != args["expected_icalendar"]:
                    raise CalendarBridgeFailure("changed", "edited", sent=True)
                if cmd == "calendar_update":
                    self.items[event_id] = str(args["icalendar"])
                else:
                    del self.items[event_id]
            return {"item": self._item(event_id)}

        def late() -> None:
            try:
                apply()
            except CalendarBridgeFailure:
                return

        if Fault.BUSY_BEFORE_APPLY in self._armed:
            self._armed.remove(Fault.BUSY_BEFORE_APPLY)
            raise CalendarBridgeFailure("busy", sent=True, mutation="none")
        if Fault.LOSE_BEFORE_APPLY in self._armed:
            self._armed.remove(Fault.LOSE_BEFORE_APPLY)
            raise CalendarBridgeFailure("timeout", sent=True, mutation="possible")
        if Fault.SLOW in self._armed:
            self._armed.remove(Fault.SLOW)
            self._late.append((self.clock.now() + timedelta(seconds=self.deadline + 60), late))
            self.clock.advance(seconds=self.deadline + 1)
            raise CalendarBridgeFailure("timeout", sent=True, mutation="possible")
        try:
            result = apply()
        except CalendarBridgeFailure as failure:
            failure.mutation = "none"
            raise
        if Fault.LOSE_RESPONSE_AFTER_APPLY in self._armed:
            self._armed.remove(Fault.LOSE_RESPONSE_AFTER_APPLY)
            raise CalendarBridgeFailure("timeout", sent=True, mutation="possible")
        return result
