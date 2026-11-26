"""Run the clean-up rules through the harness: propose the grant, then move to Trash.

Every move is an ordinary `mail.trash` write: proposed on the "Mail clean-up" card,
authorised by one standing grant per mailbox (trash only, from exactly the rules' folders,
on that card), and audited. The grant is the owner's to
confirm in the viewer: this module only proposes it.
"""

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from chamberlain.app.context import Context
from chamberlain.cleanup.rules import (
    Candidate,
    CleanupConfig,
    candidates,
    find_folder,
    sources_of,
)
from chamberlain.domain.grants import GrantState, StandingScope
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import IntentKind, MailTrash
from chamberlain.domain.operations import OperationState
from chamberlain.memory import repo as memory_repo
from chamberlain.memory.cards import CardService
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.schema import get_state, set_state
from chamberlain.writes import repo
from chamberlain.writes.core import Engine

log = logging.getLogger("chamberlain.cleanup")

CARD_TITLE: Final = "Mail clean-up"
GRANT_TITLE: Final = "Move old mail to Trash"
BUSY_TRIES: Final = 12
BUSY_WAIT_SECONDS: Final = 5.0
BATCH: Final = 250
"""Messages per write: one Thunderbird move each, well inside the add-on's write deadline."""
LOOK_AT_TRASH_OVER: Final = 100
"""More than this from one mailbox in one run: the report asks the agent to glance at Trash."""
CLEANUP_EVERY: Final = timedelta(days=1)
ACTOR: Final = "system:cleanup"
CARD_KEY: Final = "cleanup.card"
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


def _card(ctx: Context) -> str:
    """Keep the system card by handle, including grants created before the handle was stored."""
    with transaction(ctx.db):
        handle = get_state(ctx.db, CARD_KEY)
        if handle is None:
            for grant in repo.grants_in_states(ctx.db, (GrantState.ACTIVE, GrantState.PROPOSED)):
                scope = grant.scope
                if (
                    grant.card
                    and scope
                    and scope == _scope(grant.source_id, scope.folders, grant.card)
                ):
                    handle = grant.card
                    break
        old = memory_repo.get_card(ctx.db, handle) if handle else None
        if old is None or old["state"] != "open":
            handle = (
                CardService(ctx.db, ctx.clock)
                .create(
                    title=CARD_TITLE,
                    goal="Old mail the owner's clean-up rules move to Trash.",
                    rules="Only `chamberlain cleanup` works here.",
                )
                .handle
            )
        assert handle is not None
        if get_state(ctx.db, CARD_KEY) != handle:
            set_state(ctx.db, ctx.clock, CARD_KEY, handle)
        return handle


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


def _rule_folders(ctx: Context, config: CleanupConfig) -> dict[str, set[str]]:
    """Every folder any rule names, per mailbox (the grant covers exactly these)."""

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


def propose_grants(ctx: Context, config: CleanupConfig) -> list[tuple[str, str, str]]:
    """`(source, state, grant)` per mailbox; proposes what is missing. Never confirms."""
    card = _card(ctx)
    out: list[tuple[str, str, str]] = []
    for source, folders in sorted(_rule_folders(ctx, config).items()):
        scope = _scope(source, frozenset(folders), card)
        state, handle = _grant_state(ctx, source, scope)
        if state == "missing":
            made = Engine(ctx, actor=ACTOR).propose_standing(scope, card=card)
            assert made.grant is not None
            state, handle = "proposed", made.grant.handle
        out.append((source, state, handle))
    return out


def _grant_state(ctx: Context, source: str, scope: StandingScope) -> tuple[str, str]:
    best = ("missing", "")
    states = (GrantState.ACTIVE, GrantState.PROPOSED)
    for row in repo.grants_in_states(ctx.db, states, source):
        if row.scope != scope or row.card != scope.card:
            continue
        if row.state.value == "active":
            return "active", row.handle
        if row.state.value == "proposed":
            best = ("proposed", row.handle)
    return best


def run_cleanup(ctx: Context, config: CleanupConfig, *, dry_run: bool) -> CleanupReport:
    """Count, or move to Trash, what the rules name."""
    now = ctx.clock.now()
    report = CleanupReport()
    if dry_run:
        for rule in config.rules:
            report.planned[rule.name] = sum(1 for _ in candidates(ctx, rule, now))
        return report
    folders = _rule_folders(ctx, config)
    if not folders:
        return report
    card = _card(ctx)
    engine = Engine(ctx)
    active: dict[str, str] = {}
    for source, wanted in sorted(folders.items()):
        state, grant = _grant_state(ctx, source, _scope(source, frozenset(wanted), card))
        if state == "active":
            active[source] = grant
        else:
            report.waiting.append(source)
    unsure: set[str] = set()
    for rule in config.rules:
        for source, grant in active.items():
            found = list(candidates(ctx, rule, now, only=source, unsure=unsure))
            # One write per batch: one Thunderbird move, one check (never one per message).
            for start in range(0, len(found), BATCH):
                batch = found[start : start + BATCH]
                moved = _trash(engine, grant, card, source, batch, report)
                if moved is None:
                    break  # not covered: the rest of this mailbox would be refused too
                if moved:
                    where = f"{rule.name} ({source})"
                    report.moved_by[where] = report.moved_by.get(where, 0) + moved
                    report.per_mailbox[source] = report.per_mailbox.get(source, 0) + moved
    report.failed += [
        f"kept all of {source}: its folders could not be read in full" for source in sorted(unsure)
    ]
    return report


def _trash(
    engine: Engine,
    grant: str,
    card: str,
    source: str,
    batch: list[Candidate],
    report: CleanupReport,
) -> int | None:
    """Move one batch; how many moved, or None when the grant does not cover it."""
    refs = engine.ctx.services.refs
    handles = [refs.remember(HandleKind.MESSAGE, source, c.summary.locator) for c in batch]
    intent = MailTrash(source=source, messages=tuple(handles))
    attempt = 0
    while True:
        try:
            done = engine.run(intent, actor=ACTOR, grant=grant, card=card)
            if done is None:
                report.failed += ["not covered by the grant"] * len(batch)
                return None
            break
        except ChamberlainError as error:
            # Busy means Thunderbird refused before doing anything: wait and try again.
            attempt += 1
            if error.code is ErrorCode.SOURCE_BUSY and attempt < BUSY_TRIES:
                time.sleep(BUSY_WAIT_SECONDS)
                continue
            report.failed += [error.code.value] * len(batch)
            return 0
    operation = engine.view(done)
    if operation.state is not OperationState.CONFIRMED:
        report.failed += [operation.state.value] * len(batch)
        return 0
    report.failed += ["no longer there"] * operation.items_failed
    return operation.items_ok


def daily_cleanup(ctx: Context, config: CleanupConfig) -> CleanupReport | None:
    """The run an agent's `start` triggers: at most once a day, only when the owner turned it on.

    It proposes any missing grant (so the owner sees it in the viewer), then moves what the
    active grants cover. The day is claimed first, so a slow run is never doubled.
    """
    if not config.enabled or not config.rules:
        return None
    now = ctx.clock.now()
    with transaction(ctx.db):
        last = get_state(ctx.db, LAST_RUN_KEY)
        if last and now - datetime.fromisoformat(last) < CLEANUP_EVERY:
            return None
        set_state(ctx.db, ctx.clock, LAST_RUN_KEY, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    propose_grants(ctx, config)
    report = run_cleanup(ctx, config, dry_run=False)
    log.info(
        "clean-up moved %d to Trash; %d not moved; waiting: %s",
        report.moved,
        len(report.failed),
        ",".join(report.waiting) or "none",
    )
    return report


__all__ = [
    "CARD_TITLE",
    "CleanupReport",
    "daily_cleanup",
    "propose_grants",
    "run_cleanup",
]
