"""Run the clean-up rules through the harness: propose the grant, then move to Trash.

Every move is an ordinary `mail.trash` write: proposed on the "Mail clean-up" card,
authorised by one standing grant per mailbox (trash only, from exactly the rules' folders,
on that card), and audited. The grant is the owner's to
confirm in the viewer: this module only proposes it.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from chamberlain.app.core import App
from chamberlain.cleanup.rules import (
    Candidate,
    CleanupConfig,
    candidates,
    find_folder,
    sources_of,
)
from chamberlain.domain.grants import GrantState, StandingScope
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import IntentKind
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.schema import get_state, set_state
from chamberlain.writes import repo

log = logging.getLogger("chamberlain.cleanup")

CARD_TITLE: Final = "Mail clean-up"
GRANT_TITLE: Final = "Move old mail to Trash"
LOOK_AT_TRASH_OVER: Final = 100
"""More than this from one mailbox in one run: the report asks the agent to glance at Trash."""
CLEANUP_EVERY: Final = timedelta(days=1)
CLEANUP_AGENT: Final = "cleanup"
"""The clean-up's own session: its `start` never triggers the clean-up again."""
LAST_RUN_KEY: Final = "cleanup.last_run_at"


@dataclass
class CleanupReport:
    planned: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    """Dry run: what each rule would move."""
    moved_by: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    """Real run: what moved, per "rule (mailbox)"."""
    failed: list[str] = field(default_factory=lambda: list[str]())
    """One reason code per message not moved; never mail text (the lines reach the agent)."""
    waiting: list[str] = field(default_factory=lambda: list[str]())
    """Mailboxes whose grant is missing or waiting for the owner."""
    per_mailbox: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    """Real run: what moved, per mailbox."""

    @property
    def moved(self) -> int:
        return sum(self.moved_by.values())

    def lines(self, *, dry_run: bool) -> list[str]:
        if dry_run:
            out = [f"{rule}: {n}" for rule, n in self.planned.items()] or ["Nothing to move."]
            return ["Would move to Trash (nothing changed):", *out]
        return [*self.summary()] or ["Clean-up: nothing to move."]

    def summary(self) -> list[str]:
        """The report lines: only what happened or needs the owner, nothing when idle."""
        out: list[str] = []
        if self.moved:
            parts = "; ".join(f"{where} {n}" for where, n in self.moved_by.items())
            out.append(f"Clean-up moved {self.moved} emails to Trash: {parts}.")
        many = [m for m, n in self.per_mailbox.items() if n > LOOK_AT_TRASH_OVER]
        if many:
            out.append(
                f"That is more than {LOOK_AT_TRASH_OVER} from {', '.join(many)}: glance at "
                "Trash there to check nothing the owner wants went with it."
            )
        if self.waiting:
            out.append(
                "Clean-up is waiting for the owner to confirm "
                f'"{GRANT_TITLE}" in the viewer for: {", ".join(self.waiting)}.'
            )
        if self.failed:
            reasons = Counter(self.failed).most_common(3)
            why = "; ".join(f"{reason} {n}" for reason, n in reasons)
            out.append(f"Clean-up could not move {len(self.failed)} emails: {why}.")
        return out


class _Session:
    def __init__(self, app: App) -> None:
        self.app = app
        self.session = str(self.ok("start", agent=CLEANUP_AGENT)["session"])

    def ok(self, tool: str, **request: Any) -> dict[str, Any]:
        if tool != "start":
            request.setdefault("session", getattr(self, "session", None))
        reply = self.app.call(tool, request)
        if not reply.ok or reply.data is None:
            error = reply.error
            code = error.code if error is not None else ErrorCode.INTERNAL_UNEXPECTED
            message = error.message if error is not None else f"{tool} failed"
            raise ChamberlainError(code, message)
        data: dict[str, Any] = reply.data.model_dump(mode="json", exclude_none=True)
        return data

    def card(self) -> str:
        found = self.ok("find", scope="cards", query=CARD_TITLE, limit=10)
        hits: list[dict[str, Any]] = found.get("hits", [])
        for hit in hits:
            content: dict[str, Any] = hit.get("content") or {}
            text = str(content.get("text", ""))
            if text == CARD_TITLE and str(hit.get("summary", "")).startswith("open"):
                return str(hit["handle"])
        made = self.ok(
            "card",
            op="create",
            fields={
                "title": CARD_TITLE,
                "goal": "Old mail the owner's clean-up rules move to Trash.",
                "rules": "Only `chamberlain cleanup` works here.",
            },
        )
        return str(made["card"]["handle"])


def _scope(source: str, folder_ids: frozenset[str], card: str) -> StandingScope:
    return StandingScope(
        source=source,
        intent_kinds=frozenset({IntentKind.MAIL_TRASH}),
        title=GRANT_TITLE,
        summary="Move mail older than the clean-up rules allow, from exactly these folders, "
        "to Trash (recoverable there until Trash is emptied).",
        folders=folder_ids,
        card=card,
    )


def _rule_folders(app: App, config: CleanupConfig) -> dict[str, set[str]]:
    """Every folder any rule names, per mailbox (the grant covers exactly these)."""

    ctx = app.context
    by: dict[str, set[str]] = {}
    for rule in config.rules:
        for source in sources_of(ctx, rule):
            try:
                folder = find_folder(ctx.sources.mail_port(source).folders(source), rule.folder)
            except ChamberlainError:
                continue
            if folder is not None:
                by.setdefault(source, set()).add(folder.id)
    return by


def propose_grants(app: App, config: CleanupConfig) -> list[tuple[str, str, str]]:
    """`(source, state, grant)` per mailbox; proposes what is missing. Never confirms."""
    s = _Session(app)
    card = s.card()
    out: list[tuple[str, str, str]] = []
    for source, folders in sorted(_rule_folders(app, config).items()):
        scope = _scope(source, frozenset(folders), card)
        state, handle = _grant_state(app, source, scope)
        if state == "missing":
            made = s.ok(
                "propose", standing=scope.model_dump(mode="json", exclude_none=True), card=card
            )
            state, handle = "proposed", str(made["grant"])
        out.append((source, state, handle))
    return out


def _grant_state(app: App, source: str, scope: StandingScope) -> tuple[str, str]:
    best = ("missing", "")
    states = (GrantState.ACTIVE, GrantState.PROPOSED)
    for row in repo.grants_in_states(app.context.db, states, source):
        if row.scope != scope or row.card != scope.card:
            continue
        if row.state.value == "active":
            return "active", row.handle
        if row.state.value == "proposed":
            best = ("proposed", row.handle)
    return best


def run_cleanup(app: App, config: CleanupConfig, *, dry_run: bool) -> CleanupReport:
    """Count, or move to Trash, what the rules name."""
    ctx = app.context
    now = ctx.clock.now()
    report = CleanupReport()
    if dry_run:
        for rule in config.rules:
            report.planned[rule.name] = sum(1 for _ in candidates(ctx, rule, now))
        return report
    folders = _rule_folders(app, config)
    if not folders:
        return report
    s = _Session(app)
    card = s.card()
    active: list[str] = []
    for source, wanted in sorted(folders.items()):
        state, _ = _grant_state(app, source, _scope(source, frozenset(wanted), card))
        (active.append(source) if state == "active" else report.waiting.append(source))
    for rule in config.rules:
        for source in active:
            for candidate in candidates(ctx, rule, now, only=source):
                outcome = _trash(s, card, candidate, report)
                if outcome == "moved":
                    where = f"{rule.name} ({source})"
                    report.moved_by[where] = report.moved_by.get(where, 0) + 1
                    report.per_mailbox[source] = report.per_mailbox.get(source, 0) + 1
                elif outcome == "refused":
                    # Not covered: the rest of this mailbox would be refused too.
                    report.failed.append("not covered by the grant")
                    break
    return report


def _trash(
    s: _Session, card: str, candidate: Candidate, report: CleanupReport
) -> Literal["moved", "failed", "refused"]:
    refs = s.app.context.services.refs
    handle = refs.remember(HandleKind.MESSAGE, candidate.source, candidate.summary.locator)
    intent = {"kind": "mail.trash", "source": candidate.source, "message": handle}
    try:
        proposed = s.ok("propose", intent=intent, card=card)
        if not proposed.get("ready"):
            return "refused"
        done = s.ok("do", operation=proposed["operation"])
    except ChamberlainError as error:
        report.failed.append(error.code.value)
        return "failed"
    state = str(done["operation"]["state"])
    if state != "confirmed":
        report.failed.append(state)
        return "failed"
    return "moved"


def daily_cleanup(app: App, config: CleanupConfig) -> CleanupReport | None:
    """The run an agent's `start` triggers: at most once a day, only when the owner turned it on.

    It proposes any missing grant (so the owner sees it in the viewer), then moves what the
    active grants cover. The day is claimed first, so a slow run is never doubled.
    """
    ctx = app.context
    if not config.enabled or not config.rules:
        return None
    now = ctx.clock.now()
    with transaction(ctx.db):
        last = get_state(ctx.db, LAST_RUN_KEY)
        if last and now - datetime.fromisoformat(last) < CLEANUP_EVERY:
            return None
        set_state(ctx.db, ctx.clock, LAST_RUN_KEY, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    propose_grants(app, config)
    report = run_cleanup(app, config, dry_run=False)
    log.info(
        "clean-up moved %d to Trash; %d not moved; waiting: %s",
        report.moved,
        len(report.failed),
        ",".join(report.waiting) or "none",
    )
    return report


__all__ = [
    "CARD_TITLE",
    "CLEANUP_AGENT",
    "CleanupReport",
    "daily_cleanup",
    "propose_grants",
    "run_cleanup",
]
