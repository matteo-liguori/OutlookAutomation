"""An in-memory fake of the Google Calendar API, as an HTTP transport.

It follows Google's behaviour, not the contract: a PUT replaces the whole
resource, If-Match is enforced with 412, import refuses a live duplicate iCalUID,
and a deleted event stays readable as `cancelled`.
"""

import copy
import json
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from chamberlain.connectors.calendar_support.http import HttpReply, TransportFailure
from chamberlain.connectors.fake.world import Fault
from chamberlain.connectors.google_calendar import mapping
from chamberlain.ports.clock import ManualClock

Json = dict[str, Any]
PREFIX = "/calendar/v3"


def _reply(status: int, body: Json | None = None) -> HttpReply:
    return HttpReply(status, {}, json.dumps(body).encode() if body is not None else b"")


class FakeGoogleApi:
    def __init__(
        self,
        clock: ManualClock,
        calendar_id: str = "cal@group.calendar.google.com",
        owner: str = "me@example.test",
    ) -> None:
        self.clock = clock
        self.calendar_id = calendar_id
        self.owner = owner
        self.params: list[tuple[str, str, dict[str, str]]] = []
        """Every request's method, path and query, so a test can assert `sendUpdates`."""
        self.events: dict[str, Json] = {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Json] = []
        self.deadline = 30.0
        self._serial = 0
        self._armed: list[Fault] = []
        self._late: list[tuple[Any, Callable[[], HttpReply]]] = []
        self.fail_next: dict[str, int] = {}
        """`{method: status}`: answer the next such request with this status, applying nothing."""

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

    def seed(self, item: Json) -> str:
        self._serial += 1
        stored = copy.deepcopy(item)
        stored.setdefault("id", f"g{self._serial}")
        stored.setdefault("iCalUID", f"{stored['id']}@google.com")
        stored.setdefault("status", "confirmed")
        self._touch(stored)
        self.events[stored["id"]] = stored
        return str(stored["id"])

    def live(self, uid: str) -> list[Json]:
        return [
            e for e in self.events.values() if e["iCalUID"] == uid and e["status"] != "cancelled"
        ]

    def _touch(self, item: Json) -> None:
        self._serial += 1
        item["etag"] = f'"{self._serial}"'
        item["updated"] = self.clock.now().isoformat().replace("+00:00", "Z")

    # --- transport ------------------------------------------------------------

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpReply:
        parts = urlsplit(url)
        path = unquote(parts.path)
        self.calls.append((method, path))
        if not headers.get("Authorization", "").startswith("Bearer "):
            return _reply(401, {"error": "unauthenticated"})
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.params.append((method, path, params))
        data: Json | None = json.loads(body) if body else None
        if data is not None:
            self.bodies.append(data)
        if method == "GET":
            return self._get(path, params)
        if method in self.fail_next:
            return _reply(self.fail_next.pop(method), {"error": "injected"})

        def apply() -> HttpReply:
            return self._mutate(method, path, data, headers.get("If-Match"))

        if Fault.BUSY_BEFORE_APPLY in self._armed:
            self._armed.remove(Fault.BUSY_BEFORE_APPLY)
            return _reply(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
        if Fault.LOSE_BEFORE_APPLY in self._armed:
            self._armed.remove(Fault.LOSE_BEFORE_APPLY)
            raise TransportFailure("connection reset", sent=True)
        if Fault.SLOW in self._armed:
            self._armed.remove(Fault.SLOW)
            due = self.clock.now() + timedelta(seconds=self.deadline + 60)
            self._late.append((due, apply))
            self.clock.advance(seconds=self.deadline + 1)
            raise TransportFailure("timed out", sent=True)
        reply = apply()
        if Fault.LOSE_RESPONSE_AFTER_APPLY in self._armed:
            self._armed.remove(Fault.LOSE_RESPONSE_AFTER_APPLY)
            raise TransportFailure("connection reset", sent=True)
        return reply

    # --- the API --------------------------------------------------------------

    def _events_path(self) -> str:
        return f"{PREFIX}/calendars/{self.calendar_id}/events"

    def _get(self, path: str, params: dict[str, str]) -> HttpReply:
        if path == f"{PREFIX}/calendars/{self.calendar_id}":
            return _reply(200, {"id": self.calendar_id})
        base = self._events_path()
        if path == base:
            return self._list(params)
        if path.startswith(base + "/"):
            found = self.events.get(path[len(base) + 1 :])
            return _reply(200, copy.deepcopy(found)) if found else _reply(404, {"error": "none"})
        return _reply(404, {"error": "no such path"})

    def _list(self, params: dict[str, str]) -> HttpReply:
        deleted = params.get("showDeleted") == "true"
        matches = [e for e in self.events.values() if deleted or e["status"] != "cancelled"]
        if "iCalUID" in params:
            matches = [e for e in matches if e["iCalUID"] == params["iCalUID"]]
        else:
            low, high = params["timeMin"], params["timeMax"]
            low_t, _ = mapping.parse_time({"dateTime": low})
            high_t, _ = mapping.parse_time({"dateTime": high})

            def overlaps(item: Json) -> bool:
                if not mapping.has_start(item):
                    return False
                start = mapping.parse_time(item["start"])[0]
                end = mapping.parse_time(item["end"])[0]
                return end >= low_t and start <= high_t

            matches = [e for e in matches if overlaps(e)]
            if "q" in params:
                needle = params["q"].casefold()
                matches = [
                    e
                    for e in matches
                    if needle in (e.get("summary", "") + e.get("description", "")).casefold()
                ]
            matches.sort(key=lambda e: (mapping.parse_time(e["start"])[0], e["id"]))
        size = int(params.get("maxResults", "250"))
        offset = int(params.get("pageToken", "0"))
        page = matches[offset : offset + size]
        body: Json = {"items": copy.deepcopy(page)}
        if offset + size < len(matches):
            body["nextPageToken"] = str(offset + size)
        return _reply(200, body)

    def _insert(self, data: Json) -> HttpReply:
        """`events.insert`: the client may choose the id; a repeat is 409. Guests make the
        calendar's owner the organiser, listed among the attendees as Google does."""
        self._serial += 1
        item = copy.deepcopy(data)
        event_id = item.setdefault("id", f"g{self._serial}")
        if event_id in self.events:
            return _reply(409, {"error": "identifier already exists"})
        item.update(status="confirmed", iCalUID=f"{event_id}@google.com")
        if item.get("attendees"):
            item["organizer"] = {"email": self.owner, "self": True}
            item["attendees"] = [
                {
                    "email": self.owner,
                    "organizer": True,
                    "self": True,
                    "responseStatus": "accepted",
                },
                *({"responseStatus": "needsAction", **a} for a in item["attendees"]),
            ]
        self._touch(item)
        self.events[event_id] = item
        return _reply(200, item)

    def _patch(self, found: Json, data: Json) -> HttpReply:
        """A guest may change only their own answer."""
        item = copy.deepcopy(found)
        if "attendees" in data and not found.get("organizer", {}).get("self"):
            old = {a["email"]: a for a in found.get("attendees", [])}
            for entry in data["attendees"]:
                before = old.get(entry["email"])
                if before is None or (
                    not before.get("self")
                    and before.get("responseStatus") != entry.get("responseStatus")
                ):
                    return _reply(403, {"error": "forbidden"})
        item.update(copy.deepcopy(data))
        self._touch(item)
        self.events[found["id"]] = item
        return _reply(200, item)

    def _mutate(self, method: str, path: str, data: Json | None, if_match: str | None) -> HttpReply:
        base = self._events_path()
        if method == "POST" and path == base and data is not None:
            return self._insert(data)
        if method == "POST" and path == base + "/import" and data is not None:
            if self.live(data.get("iCalUID", "")):
                return _reply(409, {"error": "duplicate"})
            self._serial += 1
            item = copy.deepcopy(data)
            item.update(id=f"g{self._serial}", status="confirmed")
            self._touch(item)
            self.events[item["id"]] = item
            return _reply(200, item)
        event_id = path[len(base) + 1 :] if path.startswith(base + "/") else ""
        found = self.events.get(event_id)
        if found is None:
            return _reply(404, {"error": "none"})
        if if_match is not None and found["etag"] != if_match:
            return _reply(412, {"error": "precondition"})
        if method == "PUT" and data is not None:
            item = copy.deepcopy(data)
            item.update(id=found["id"], status=data.get("status", "confirmed"))
            item.setdefault("iCalUID", found["iCalUID"])
            if "organizer" in found:
                item["organizer"] = found["organizer"]
            self._touch(item)
            self.events[found["id"]] = item
            return _reply(200, item)
        if method == "PATCH" and data is not None:
            return self._patch(found, data)
        if method == "DELETE":
            found["status"] = "cancelled"
            self._touch(found)
            return _reply(204)
        return _reply(405)
