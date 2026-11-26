"""The lab: what a scenario uses to act, wait, check and clean up.

Contract: docs/live-test.md (How a scenario runs). Every action goes through `App.call`, the
same entry the agent uses. The probe (read-only, over the ports) answers only the checks the
tools cannot make, and every such check is named in the report. Nothing here confirms a
grant: a write the standing grants do not cover stops the scenario.
"""

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, cast

from chamberlain.app.core import App
from chamberlain.app.wiring import run_tick
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.mail import FolderRole
from chamberlain.livetest.config import LiveTestConfig
from chamberlain.livetest.env import CalendarCopy, Environment, MailCopy, strip_reply_prefix
from chamberlain.livetest.markers import MARKER, carries_marker, run_marker
from chamberlain.livetest.model import Timing
from chamberlain.schema.errors import ChamberlainError

JSON = dict[str, Any]

_HANDLE = re.compile(r"\b((?:op|msg|cev|art|grant)_[a-z0-9]{8,32})\b")
POLL_SECONDS = 3
MAIL_CLEANUP_PASSES = 4
_OWN_TOOLS = frozenset({"propose", "do", "reconcile", "keep", "open", "forget"})
"""Tools whose replies and requests name only things this run made."""
CALENDAR_WINDOW_BEFORE = timedelta(days=60)
CALENDAR_WINDOW_AFTER = timedelta(days=550)


class ScenarioFailure(Exception):
    """An assertion about the harness did not hold. The text names what was seen."""


class RepeatRefused(ScenarioFailure):
    """The same write was done in the last 24 hours and the harness will not repeat it."""


class ScenarioSkip(Exception):
    """The scenario cannot run here. The text is the reason shown in the report."""


class RunStopped(Exception):
    """The whole run cannot go on (for example a missing grant)."""


@dataclass(frozen=True)
class Call:
    """One tool reply, reduced to what the scenarios read."""

    tool: str
    ok: bool
    data: JSON
    error: JSON
    raw: JSON
    chars: int

    @property
    def code(self) -> str:
        return str(self.error.get("code", ""))

    @property
    def guidance_chars(self) -> int:
        """Characters of guidance text in this reply (a pointer counts as its own text)."""
        total = 0
        for item in self.raw.get("guidance", []):
            total += len(str(item.get("text") or item.get("more") or ""))
        return total


@dataclass
class Lab:
    """One run's session, card and recorders."""

    app: App
    env: Environment
    config: LiveTestConfig
    run_id: str
    approve_edits: bool
    approval_source: str
    sweeping: bool = False
    """A sweep removes marked items of any run, so its approval names the marker, not one run."""
    session: str = ""
    card: str = ""
    action: str = ""
    baseline: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    """`{doctor check id: status}` taken before the first scenario."""
    run_handles: set[str] = field(default_factory=lambda: set[str]())
    """Every operation, message and event handle of the whole run."""
    accepted_uncertain: set[str] = field(default_factory=lambda: set[str]())
    """Operations a fault scenario left uncertain on purpose (and says so)."""
    handles: set[str] = field(default_factory=lambda: set[str]())
    timings: list[Timing] = field(default_factory=lambda: list[Timing]())
    probe_checks: list[str] = field(default_factory=lambda: list[str]())
    _cleanups: list[tuple[str, Callable[[], object]]] = field(
        default_factory=lambda: list[tuple[str, Callable[[], object]]]()
    )
    write_lock: Any = field(default_factory=threading.Lock)
    """Shared by the labs of one run: Thunderbird has one write slot, so writes take turns
    while reads and waits of scenarios running side by side overlap."""

    # --- recording ------------------------------------------------------------------

    def begin(self) -> None:
        self.handles = set()
        self.timings = []
        self.probe_checks = []
        self._cleanups = []

    @property
    def marker(self) -> str:
        return run_marker(self.run_id)

    @property
    def approval_prefix(self) -> str:
        return MARKER if self.sweeping else self.marker

    def subject(self, scenario: str) -> str:
        return f"{self.marker} {scenario}"

    def track(self, handle: str) -> None:
        """Record a handle this run made or found by its marker."""
        self.handles.add(handle)
        self.run_handles.add(handle)

    def skip(self, reason: str) -> None:
        raise ScenarioSkip(reason)

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            raise ScenarioFailure(message)

    def used_probe(self, what: str) -> None:
        if what not in self.probe_checks:
            self.probe_checks.append(what)

    # --- tools ----------------------------------------------------------------------

    def call(self, tool: str, **request: Any) -> Call:
        if self.session and "session" not in request and tool != "start":
            request["session"] = self.session
        started = time.monotonic()
        if tool == "do":
            with self.write_lock:
                reply = self.app.call(tool, request)
        else:
            reply = self.app.call(tool, request)
        elapsed = time.monotonic() - started
        raw: JSON = reply.model_dump(mode="json", exclude_none=True)
        text = json.dumps(raw)
        self.timings.append(Timing(tool=tool, ms=round(elapsed * 1000)))
        if tool in _OWN_TOOLS:
            # start, next and doctor list the owner's own work: never count those handles
            for found in _HANDLE.findall(text) + _HANDLE.findall(json.dumps(request, default=str)):
                if not found.startswith("grant_"):
                    self.track(found)
        data: JSON = raw.get("data") or {}
        error: JSON = raw.get("error") or {}
        return Call(tool, bool(raw.get("ok")), data, error, raw, len(text))

    def ok(self, tool: str, **request: Any) -> JSON:
        """A call that must succeed; its `data`."""
        result = self.call(tool, **request)
        if not result.ok:
            raise ScenarioFailure(f"{tool} failed: {result.code} {result.error.get('message', '')}")
        return result.data

    def wait_until[T](self, what: str, probe: Callable[[], T | None], *, seconds: int = 0) -> T:
        """Poll until `probe` returns something; fail naming what never happened."""
        limit = seconds or self.config.arrival_wait_seconds
        waited = 0
        while True:
            found = probe()
            if found is not None:
                return found
            if waited >= limit:
                raise ScenarioFailure(f"{what} did not happen within {limit} seconds")
            self.env.waiter.sleep(POLL_SECONDS)
            waited += POLL_SECONDS

    # --- mail -----------------------------------------------------------------------

    def hits(self, source: str, query: str, *, folder: str | None = None) -> list[JSON]:
        request: JSON = {"scope": "mail", "source": source, "query": query, "limit": 25}
        if folder is not None:
            request["folder"] = folder
        data = self.ok("find", **request)
        # Thunderbird reports a reply's subject without its "Re:" (found live, 2026-11-25).
        wanted = strip_reply_prefix(query)
        found = [hit for hit in data.get("hits", []) if wanted in _line(hit)]
        for hit in found:
            self.track(str(hit["handle"]))
        return found

    def arrival(self, source: str, subject: str) -> JSON:
        """The inbox hit for a marked subject, waiting for it to arrive."""

        def look() -> JSON | None:
            self.env.probe.sync_inbox(source)
            return next(iter(self.hits(source, subject, folder="inbox")), None)

        return self.wait_until(f"the mail arriving in {source}", look)

    def message_id(self, handle: str) -> str:
        """The Message-ID header of a message, read through `open headers`."""
        opened = self.ok("open", handle=handle, part="headers")
        for line in text_of(opened).splitlines():
            if line.lower().startswith("message-id:"):
                return line.split(":", 1)[1].strip()
        raise ScenarioFailure("open headers shows no Message-ID")

    def copies(self, source: str, subject: str) -> list[MailCopy]:
        self.used_probe("copy counts across folders")
        return self.env.probe.copies(source, subject)

    def folder_id(self, source: str, role: FolderRole) -> str:
        for folder in self.env.probe.folders(source):
            if folder.role is role:
                return folder.id
        raise ScenarioSkip(f"{source} has no {role.value} folder")

    def handle_of(self, copy: MailCopy) -> str:
        """A `msg_` handle for a copy found by the probe (cleanup and sweep only)."""
        handle = self.app.context.services.refs.remember(
            HandleKind.MESSAGE, copy.source, copy.locator, copy.message_id
        )
        self.track(handle)
        return handle

    # --- writes ---------------------------------------------------------------------

    def prepare(self, intent: JSON) -> str:
        """propose, then attach the permission that already exists. Returns the operation.

        A standing grant covers mail. Calendar writes need the owner's recorded approval
        (config or option); without it the scenario is skipped, never approved here.
        """
        kind = str(intent["kind"])
        first = self.call("propose", intent=intent, card=self.card)
        if not first.ok:
            raise ScenarioFailure(f"propose {kind} failed: {first.code}")
        proposed = first.data
        operation = str(proposed["operation"])
        if proposed["ready"]:
            return operation
        if proposed["requirement"] == "none":
            raise RepeatRefused(f"{kind} repeats a write done in the last 24 hours")
        if proposed["requirement"] != "agent_evidence" or not kind.startswith("calendar."):
            self._refused(operation, kind, str(proposed["requirement"]))
            raise ScenarioFailure(
                f"{kind} is not covered by a confirmed grant "
                f"(needs {proposed['requirement']}); run `chamberlain live-test grant`"
            )
        if not self.approve_edits:
            raise ScenarioSkip(
                "calendar writes need the owner's approval: set "
                "live_test.approve_calendar_edits or pass --approve-calendar-edits"
            )
        text = (
            f"Owner approval (from {self.approval_source}) for live test "
            f"{'sweep' if self.sweeping else 'run'} {self.run_id}: change or delete only "
            f"calendar events whose title starts with '{self.approval_prefix}'. "
            f"Operation {operation}."
        )
        evidence = self.ok(
            "keep",
            kind="evidence",
            owner=operation,
            text=text,
            reason="Owner's standing approval for the live test.",
        )
        approved = self.ok(
            "propose",
            operation=operation,
            approval_evidence=evidence["handle"],
            card=self.card,
        )
        if not approved["ready"]:
            raise ScenarioFailure(f"the approval did not make {operation} ready")
        return operation

    def _refused(self, operation: str, kind: str, requirement: str) -> None:
        """A not-ready write: ask `do` why (it re-checks authority and refuses to send).

        The only reason that is not a defect is the hourly or daily limit, which is a skip.
        """
        refused = self.call("do", operation=operation)
        if refused.ok:
            raise ScenarioFailure(f"{kind} ran although the proposal was not ready")
        if refused.code == "grant.limit_reached":
            raise ScenarioSkip("the hourly or daily send limit of the grant was reached")

    def do(self, operation: str) -> Call:
        done = self.call("do", operation=operation)
        if done.code == "grant.limit_reached":
            raise ScenarioSkip("the hourly or daily send limit of the grant was reached")
        return done

    def write(self, intent: JSON) -> Call:
        """propose, authorise, then `do` once."""
        return self.do(self.prepare(intent))

    def tick(self) -> None:
        """The operator's tick, forced (the same call `chamberlain tick` makes)."""
        run_tick(self.app)

    def confirmed(self, done: Call, what: str) -> JSON:
        """The operation view of a `do` that must have ended confirmed."""
        if not done.ok:
            raise ScenarioFailure(f"{what}: do failed with {done.code}")
        view: JSON = done.data["operation"]
        if view["state"] != "confirmed":
            raise ScenarioFailure(f"{what}: operation ended {view['state']}, not confirmed")
        return view

    def send(
        self,
        source: str,
        to: str,
        subject: str,
        body: str,
        *,
        attachments: list[JSON] | None = None,
        reply_to: str | None = None,
    ) -> JSON:
        intent: JSON = {
            "kind": "mail.send",
            "source": source,
            "to": [to],
            "subject": subject,
            "body": body,
        }
        if attachments:
            intent["attachments"] = attachments
        if reply_to is not None:
            intent["reply_to"] = reply_to
        return self.confirmed(self.write(intent), f"send from {source}")

    def trash(self, handle: str) -> JSON:
        """Trash a message. The harness refuses an identical repeat inside 24 hours (a message
        trashed, restored and trashed again), so that case files it into the trash folder."""
        source = _source_of(self, handle)
        try:
            done = self.write({"kind": "mail.trash", "source": source, "messages": [handle]})
        except RepeatRefused:
            done = self.write(
                {
                    "kind": "mail.move",
                    "source": source,
                    "messages": [handle],
                    "to_folder": self.folder_id(source, FolderRole.TRASH),
                }
            )
        return self.confirmed(done, "trash")

    # --- cleanup --------------------------------------------------------------------

    def defer(self, description: str, action: Callable[[], object]) -> None:
        self._cleanups.append((description, action))

    def cleanup(self) -> list[str]:
        """Run every deferred cleanup, last first. Returns what could not be removed."""
        problems: list[str] = []
        while self._cleanups:
            description, action = self._cleanups.pop()
            try:
                action()
            except (ScenarioFailure, ScenarioSkip, Exception) as error:
                problems.append(f"{description}: {str(error)[:100]}")
        return problems

    def remove_mail(self, source: str, text: str) -> int:
        """Trash every copy of marked mail matching `text` (not already in the trash).

        Only a subject that opens with the marker is touched. One batch write per folder (one
        Thunderbird move, one check), never one write per copy. Each pass lists the copies again
        (trashing one copy can remove or merge another, as on Gmail); a few passes at most.
        """
        removed = 0
        failures: list[str] = []
        for _ in range(MAIL_CLEANUP_PASSES):
            by_folder: dict[str, list[MailCopy]] = {}
            for copy in self.copies(source, text):
                if copy.folder_role is not FolderRole.TRASH and carries_marker(copy.subject):
                    by_folder.setdefault(copy.folder_id, []).append(copy)
            if not by_folder:
                break
            progress = False
            for batch in by_folder.values():
                try:
                    moved = self._trash_batch(source, batch)
                except (ScenarioFailure, ChamberlainError) as error:
                    failures.append(str(error)[:80])
                    continue
                removed += moved
                progress = progress or moved > 0
            if not progress:
                break
        left = [
            c
            for c in self.copies(source, text)
            if c.folder_role is not FolderRole.TRASH and carries_marker(c.subject)
        ]
        if left:
            raise ScenarioFailure(
                f"{len(left)} marked message(s) remain in {source}"
                + (f" ({failures[-1]})" if failures else "")
            )
        return removed

    def _trash_batch(self, source: str, copies: list[MailCopy]) -> int:
        """One `mail.trash` write for these copies of one folder; how many moved."""
        handles = list(dict.fromkeys(self.handle_of(copy) for copy in copies))
        if len(handles) == 1:
            self.trash(handles[0])
            return 1
        try:
            done = self.write({"kind": "mail.trash", "source": source, "messages": handles})
        except RepeatRefused:
            # The same set was trashed within 24 hours (restored since): one at a time.
            for handle in handles:
                self.trash(handle)
            return len(handles)
        view = self.confirmed(done, "trash")
        return int(view.get("items_ok", len(handles)))

    def calendar_window(self) -> tuple[datetime, datetime]:
        now = self.env.clock.now()
        return now - CALENDAR_WINDOW_BEFORE, now + CALENDAR_WINDOW_AFTER

    def remove_events(self, source: str, text: str) -> int:
        """Delete every live event whose title opens with the marker and contains `text`."""
        start, end = self.calendar_window()
        self.used_probe("calendar events including cancelled")
        removed = 0
        deleted: set[str] = set()
        for item in self.env.probe.events(source, text, start, end):
            if item.status == "cancelled" or not carries_marker(item.title):
                continue
            if item.series is not None:
                # A date of a series: the series goes once, through its own handle (P-25).
                if item.series in deleted:
                    continue
                deleted.add(item.series)
                item = replace(item, locator=item.series)
            handle = self.event_handle(item)
            opened = self.ok("open", handle=handle)
            intent: JSON = {
                "kind": "calendar.delete",
                "source": source,
                "event_ref": handle,
                "expected_etag": str(opened["view"]["etag"]),
            }
            if opened["view"].get("repeats") == "series":
                intent["scope"] = "series"
            self.confirmed(self.write(intent), "delete event")
            removed += 1
        return removed

    def event_handle(self, item: CalendarCopy) -> str:
        handle = self.app.context.services.refs.remember(
            HandleKind.CALENDAR_EVENT, item.source, item.locator, None
        )
        self.track(handle)
        return handle


def text_of(item: JSON) -> str:
    """The text of an item's `content` (a hit line or an opened part), or empty."""
    content: object = item.get("content")
    if isinstance(content, dict):
        return str(cast("JSON", content).get("text", ""))
    return ""


def _line(hit: JSON) -> str:
    return text_of(hit)


def hit_line(hit: JSON) -> str:
    return _line(hit)


def _source_of(lab: Lab, handle: str) -> str:
    source, _ = lab.app.context.services.refs.locate(handle)
    return source
