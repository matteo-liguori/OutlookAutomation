"""Run the clean-up rules through the harness: propose the grant, then move to Trash.

Every move is an ordinary `mail.trash` write: proposed on the "Mail clean-up" card,
authorised by one standing grant per mailbox (trash only, from exactly the rules' folders,
on that card, at most `MAX_PER_DAY` a day), and audited. The grant is the owner's to
confirm in the viewer: this module only proposes it.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

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
MAX_PER_DAY: Final = 500
CLEANUP_EVERY: Final = timedelta(days=1)
LAST_RUN_KEY: Final = "cleanup.last_run_at"


@dataclass
class CleanupReport:
    planned: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    moved: int = 0
    failed: list[str] = field(default_factory=lambda: list[str]())
    waiting: list[str] = field(default_factory=lambda: list[str]())
    """Mailboxes whose grant is missing or waiting for the owner."""

    def lines(self, *, dry_run: bool) -> list[str]:
        out = [f"{rule}: {count}" for rule, count in self.planned.items()] or ["Nothing to move."]
        if dry_run:
            return ["Would move to Trash (nothing changed):", *out]
        out = ["Planned:", *out, f"Moved to Trash: {self.moved}"]
        if self.waiting:
            out.append("Waiting for your confirmation in the viewer: " + ", ".join(self.waiting))
        if self.failed:
            out.append(f"Not moved ({len(self.failed)}): " + "; ".join(self.failed[:5]))
        return out


class _Session:
    def __init__(self, app: App) -> None:
        self.app = app
        self.session = str(self.ok("start", agent="cleanup")["session"])

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
        max_per_day=MAX_PER_DAY,
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
    """Count, or move to Trash, what the rules name; at most `config.per_run` per mailbox."""
    ctx = app.context
    now = ctx.clock.now()
    report = CleanupReport()
    found: list[Candidate] = []
    for rule in config.rules:
        mine = list(candidates(ctx, rule, now))
        report.planned[f"{rule.name}"] = len(mine)
        found += mine
    if dry_run or not found:
        return report
    s = _Session(app)
    card = s.card()
    folders = _rule_folders(app, config)
    active: set[str] = set()
    for source, wanted in folders.items():
        state, _ = _grant_state(app, source, _scope(source, frozenset(wanted), card))
        (active.add(source) if state == "active" else report.waiting.append(source))
    refs = ctx.services.refs
    taken: dict[str, int] = {}
    for candidate in found:
        if candidate.source not in active or taken.get(candidate.source, 0) >= config.per_run:
            continue
        taken[candidate.source] = taken.get(candidate.source, 0) + 1
        handle = refs.remember(HandleKind.MESSAGE, candidate.source, candidate.summary.locator)
        intent = {"kind": "mail.trash", "source": candidate.source, "message": handle}
        try:
            proposed = s.ok("propose", intent=intent, card=card)
            if not proposed.get("ready"):
                report.failed.append(f"{candidate.summary.subject[:40]}: not covered")
                continue
            done = s.ok("do", operation=proposed["operation"])
            if done["operation"]["state"] == "confirmed":
                report.moved += 1
            else:
                report.failed.append(
                    f"{candidate.summary.subject[:40]}: {done['operation']['state']}"
                )
        except ChamberlainError as error:
            report.failed.append(f"{candidate.summary.subject[:40]}: {error.code.value}")
    return report


def daily_cleanup(app: App, config: CleanupConfig) -> CleanupReport | None:
    """The background run: at most once a day, only when the owner turned it on.

    It proposes any missing grant (so the owner sees it in the viewer), then moves what the
    active grants cover. The day is claimed at the start, so a slow run is never doubled.
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


__all__ = ["CARD_TITLE", "CleanupReport", "daily_cleanup", "propose_grants", "run_cleanup"]
