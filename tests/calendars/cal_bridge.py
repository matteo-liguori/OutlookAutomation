"""A fake Thunderbird calendar bridge: the commands of docs/contracts/12-bridge-v2.md (calendar)."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import JsonValue

from chamberlain.connectors.fake.world import Fault
from chamberlain.connectors.thunderbird import protocol as wire
from chamberlain.connectors.thunderbird.calendar import Json
from chamberlain.connectors.thunderbird.client import BridgeClient, BridgeFailure, Reply
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ErrorCode


class FakeCalendarBridge(BridgeClient):
    def __init__(
        self,
        clock: ManualClock,
        calendar_id: str = "cal-1",
        *,
        version: int = 3,
        calendar_available: bool = True,
    ) -> None:
        self.clock = clock
        self.calendar_id = calendar_id
        self.version = version
        self.calendar_available = calendar_available
        self.items: dict[str, str] = {}
        self.generated_uid: str | None = None
        self.own_appointments: set[str] = set()
        self.occurrences: list[tuple[str, str, str]] = []
        """`(event id, recurrence id, icalendar)` for series: the query answers with these."""
        self.requests: list[tuple[str, bool]] = []
        self.offline = False
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

    def hello(self) -> wire.Welcome:
        if self.offline:
            raise BridgeFailure(ErrorCode.SOURCE_UNAVAILABLE, "not connected", sent=False)
        return wire.Welcome(
            v=self.version,
            id="calendar_test",
            host_version="test",
            addon_version="test",
            calendar_available=self.calendar_available,
        )

    def call(
        self,
        cmd: wire.Command,
        args: Model,
        *,
        payloads: Mapping[str, bytes] | None = None,
        calendar: bool = False,
    ) -> Reply:
        assert calendar
        data = self.request(
            cmd.value,
            args.model_dump(mode="json", exclude_none=True),
            write=cmd in wire.WRITE_COMMANDS,
        )
        return Reply(wire.parse_result(cmd, data))

    def request(self, cmd: str, args: Mapping[str, JsonValue], *, write: bool = False) -> Json:
        self.requests.append((cmd, write))
        if self.offline:
            raise BridgeFailure(ErrorCode.SOURCE_UNAVAILABLE, "not connected", sent=False)
        if args.get("calendar_id", self.calendar_id) != self.calendar_id:
            raise BridgeFailure(ErrorCode.REQUEST_UNKNOWN_HANDLE, "no such calendar", sent=True)
        if cmd == "status":
            return {
                "write_busy": bool(self._late),
                "addon_version": "test",
                "calendar_available": True,
            }
        if cmd == "calendar_list":
            entry: Json = {
                "id": self.calendar_id,
                "name": "Calendar",
                "type": "owl",
                "disabled": False,
                "read_only": False,
            }
            return {"calendars": [entry]}
        if cmd == "calendar_query":
            return self._query(args)
        event_id = str(args["event_id"])
        if cmd == "calendar_read":
            return {"item": self._item(event_id)}
        return self._write(str(args["kind"]), event_id, args)

    # --- internals ------------------------------------------------------------

    def _item(self, event_id: str) -> Json | None:
        text = self.items.get(event_id)
        if text is None:
            return None
        item: Json = {"id": event_id, "recurrence_id": None, "icalendar": text}
        if event_id in self.own_appointments:
            item["own_appointment"] = True
        return item

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
            if cmd == "create":
                if event_id in self.items:
                    raise BridgeFailure(
                        ErrorCode.REMOTE_CHANGED,
                        "exists",
                        sent=True,
                        bridge_code=wire.BridgeErrorCode.CHANGED,
                    )
                actual = self.generated_uid or event_id
                self.items[actual] = str(args["icalendar"]).replace(
                    f"UID:{event_id}\r\n", f"UID:{actual}\r\n", 1
                )
                return {"item": self._item(actual)}
            else:
                if self.items.get(event_id) != args["expected_icalendar"]:
                    raise BridgeFailure(
                        ErrorCode.REMOTE_CHANGED,
                        "edited",
                        sent=True,
                        bridge_code=wire.BridgeErrorCode.CHANGED,
                    )
                if cmd == "update":
                    self.items[event_id] = str(args["icalendar"])
                else:
                    del self.items[event_id]
            return {"item": self._item(event_id)}

        def late() -> None:
            try:
                apply()
            except BridgeFailure:
                return

        if Fault.BUSY_BEFORE_APPLY in self._armed:
            self._armed.remove(Fault.BUSY_BEFORE_APPLY)
            raise BridgeFailure(
                ErrorCode.SOURCE_BUSY,
                "busy",
                sent=True,
                mutation="none",
                bridge_code=wire.BridgeErrorCode.BUSY,
            )
        if Fault.LOSE_BEFORE_APPLY in self._armed:
            self._armed.remove(Fault.LOSE_BEFORE_APPLY)
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE, "timeout", sent=True, mutation="possible"
            )
        if Fault.SLOW in self._armed:
            self._armed.remove(Fault.SLOW)
            self._late.append((self.clock.now() + timedelta(seconds=self.deadline + 60), late))
            self.clock.advance(seconds=self.deadline + 1)
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE, "timeout", sent=True, mutation="possible"
            )
        try:
            result = apply()
        except BridgeFailure as failure:
            failure.mutation = "none"
            raise
        if Fault.LOSE_RESPONSE_AFTER_APPLY in self._armed:
            self._armed.remove(Fault.LOSE_RESPONSE_AFTER_APPLY)
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE, "timeout", sent=True, mutation="possible"
            )
        return result
