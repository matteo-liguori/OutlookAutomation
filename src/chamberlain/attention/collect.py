"""Collect what detectors look at: store snapshots and bounded read-only source deltas.

Contract: docs/contracts/07-attention.md (A-6, A-7, A-8) and 08-ports.md (P-3, P-5).
A source that raises is logged and becomes a coverage gap, never a failed call.
"""

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import timedelta

from chamberlain.app.context import Context
from chamberlain.attention import delivery_check, repo
from chamberlain.attention.models import (
    BudgetRow,
    CalendarSlot,
    ComplexDecision,
    DetectorInput,
    SourceSnapshot,
    SyncGap,
)
from chamberlain.attention.suggest import Suggester, card_links
from chamberlain.attention.tuning import (
    CONFLICT_PAGE_LIMIT,
    CONFLICT_PAGES,
    CONFLICT_WINDOW,
    NOTE_OPEN,
)
from chamberlain.attention.watermark import scan
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery, Response
from chamberlain.domain.capabilities import HealthState, SourceHealth
from chamberlain.domain.timefmt import ts
from chamberlain.limits import TOTAL_ARTIFACT_BYTES, UNCERTAIN_SEND_RECHECK_HOURS
from chamberlain.memory import repo as memory_repo
from chamberlain.memory.repo import ActionLine, CardRow
from chamberlain.ports.page import pages
from chamberlain.review import repo as review_repo
from chamberlain.runs import repo as runs_repo
from chamberlain.schema.safelog import log_failure
from chamberlain.store.migrate import stamp
from chamberlain.store.schema import get_state
from chamberlain.writes import repo as writes_repo

log = logging.getLogger("chamberlain.attention")


@dataclass(frozen=True)
class Collected:
    data: DetectorInput
    unreadable: frozenset[str]
    """Sources whose mail could not be fully read this tick."""
    unreadable_calendars: frozenset[str] = frozenset()
    """Calendars that could not be fully read this tick; their conflicts are left as they are."""


def source_health(ctx: Context, source: str) -> SourceHealth | None:
    """Ask the owning port; None when the port raises (A-7). A `stale` mailbox may get the
    fallback delivery check, which can only turn it `ok`."""
    try:
        health = ctx.sources.port(source).health(source)
    except Exception:
        log_failure(log, "health check failed for %s", source)
        return None
    return delivery_check.confirm(ctx, source, health)  # only a `stale` mailbox, 3-hourly at most


def _health(ctx: Context) -> tuple[SourceSnapshot, ...]:
    snapshots: list[SourceSnapshot] = []
    for source in ctx.sources.daily_source_ids():
        health = source_health(ctx, source)
        state = HealthState.UNAVAILABLE if health is None else health.state
        fresh = None if health is None or health.fresh_as_of is None else health.fresh_as_of
        repo.write_health(
            ctx.db,
            source,
            state,
            stamp(ctx.clock),
            None if fresh is None else ts(fresh),
        )
        snapshots.append(SourceSnapshot(source, state))
    return tuple(snapshots)


def _mail(ctx: Context, snapshots: tuple[SourceSnapshot, ...]) -> set[str]:
    """Scan every mail source from its watermark; return the sources not fully covered."""
    unreadable: set[str] = set()
    states = {s.id: s.state for s in snapshots}
    suggester = Suggester(ctx)
    for source in ctx.sources.daily_mail():
        try:
            if not scan(ctx, source, states.get(source), suggester):
                unreadable.add(source)
        except Exception:
            log_failure(log, "mail scan failed for %s", source)
            unreadable.add(source)
    return unreadable


def _slot(event: CalendarEvent) -> CalendarSlot | None:
    """The event as a time slot, or None when it cannot clash with anything."""
    fields = event.fields
    if fields.all_day or fields.end <= fields.start or event.my_response is Response.DECLINED:
        return None
    key = f"{fields.title}|{fields.start.isoformat()}|{fields.end.isoformat()}"
    return CalendarSlot(
        event.source,
        event.locator,
        event.uid,
        fields.start,
        fields.end,
        hashlib.sha256(key.encode()).hexdigest()[:16],
        title=fields.title,
        location=fields.location,
    )


def _read_calendar(ctx: Context, source: str) -> list[CalendarSlot] | None:
    """Every timed event of one calendar from now on, or None when it was not fully read."""
    port = ctx.sources.calendar_port(source)
    now = ctx.clock.now()
    query = CalendarQuery(
        start=now, end=now + CONFLICT_WINDOW, limit=CONFLICT_PAGE_LIMIT, include_cancelled=False
    )
    slots: list[CalendarSlot] = []
    for page in pages(lambda cursor: port.events(source, query, cursor), limit=CONFLICT_PAGES):
        for event in page.items:
            if ctx.sources.is_hidden_event_title(event.fields.title):
                continue
            slot = _slot(event)
            if slot is not None:
                slots.append(slot)
        if page.gaps:
            return None
        if page.cursor is None:
            return slots if page.complete else None
    return None


def _calendars(
    ctx: Context, snapshots: tuple[SourceSnapshot, ...]
) -> tuple[tuple[CalendarSlot, ...], set[str]]:
    """The owner's timed events across every calendar, and the calendars not fully read.

    Events from a calendar kept for information (`commitments: false`) are marked, so the clash
    check can tell a definite clash from a possible one the agent judges (A-26)."""
    states = {s.id: s.state for s in snapshots}
    slots: list[CalendarSlot] = []
    unreadable: set[str] = set()
    for source in ctx.sources.daily_calendars():
        if states.get(source) is not HealthState.OK:
            unreadable.add(source)
            continue
        try:
            read = _read_calendar(ctx, source)
        except Exception:
            log_failure(log, "calendar read failed for %s", source)
            read = None
        if read is None:
            unreadable.add(source)
        elif source in ctx.sources.information_only:
            slots.extend(replace(slot, information_only=True) for slot in read)
        else:
            slots.extend(read)
    return tuple(slots), unreadable


def _sync_gaps(ctx: Context) -> tuple[SyncGap, ...]:
    by_source: dict[str, list[repo.SyncFolder]] = {}
    for source, folder in repo.sync_failures(ctx.db):
        by_source.setdefault(source, []).append(folder)
    return tuple(
        SyncGap(
            source,
            tuple(sorted(f.role for f in folders)),
            min(f.synced_at or f.attempted_at for f in folders),
        )
        for source, folders in by_source.items()
    )


UNSETTLED_AFTER = timedelta(hours=UNCERTAIN_SEND_RECHECK_HOURS)
"""How long the harness alone looks for an uncertain write before the agent is told (A-4)."""


def due_actions(db: sqlite3.Connection, now: str) -> list[ActionLine]:
    """Open actions on open cards that are due, or whose snooze has ended."""
    return memory_repo.action_lines(
        db,
        "c.state = 'open' AND ((a.due_at IS NOT NULL AND a.due_at <= ?) "
        "OR (a.snooze_until IS NOT NULL AND a.snooze_until <= ?))",
        (now, now),
    )


def _cards_needing_review(db: sqlite3.Connection, now: str) -> list[CardRow]:
    return memory_repo.card_rows(
        db.execute(
            "SELECT c.handle, c.title, c.review_at, c.over_cap, EXISTS ("
            "SELECT 1 FROM imported_context i JOIN artifacts a ON a.handle = i.artifact "
            "JOIN artifact_owners o ON o.artifact = a.handle AND o.owner_kind = 'card' "
            "AND o.owner = c.handle WHERE i.card = c.handle AND a.state = 'live' "
            "AND a.expires_at > ?) AS has_context FROM cards c WHERE c.state = 'open' "
            "AND (c.over_cap = 1 OR (c.review_at IS NOT NULL AND c.review_at <= ?))",
            (now, now),
        )
    )


def _complex_decisions(db: sqlite3.Connection, since: str) -> list[ComplexDecision]:
    """Answers the owner gave to a larger-limit request since `since`, for open cards."""
    rows = db.execute(
        "SELECT x.card, x.state, x.decided_at, x.blocked_until FROM complex_cards x "
        "JOIN cards c ON c.handle = x.card WHERE c.state = 'open' "
        "AND x.state IN ('approved', 'declined') AND x.decided_at >= ? ORDER BY x.card",
        (since,),
    )
    return [ComplexDecision(*row) for row in rows]


def _over_budget(db: sqlite3.Connection) -> list[BudgetRow]:
    """Memory writes `memory.over_budget` as "0" or "<scope>:<bytes>/<limit>[,...]"."""
    value = memory_repo.over_budget_flag(db)
    if value in ("", "0"):
        return []
    found: list[BudgetRow] = []
    for part in value.split(","):
        scope, _, figures = part.rpartition(":")
        used, _, limit = figures.partition("/")
        if used.isdigit() and limit.isdigit():
            card = scope if scope.startswith("card_") else None
            found.append(BudgetRow(card, int(used), int(limit)))
    if not found:
        found.append(BudgetRow(None, 0, TOTAL_ARTIFACT_BYTES))
    return found


def _orphan_batch(db: sqlite3.Connection) -> tuple[str | None, int]:
    """Latest scan with held orphans, and how many are held (M-26)."""
    scan = get_state(db, "memory.last_orphan_scan_at")
    if scan is None:
        return None, 0
    row = db.execute(
        "SELECT COUNT(*) AS n FROM quarantine WHERE state = 'held' AND found_at = ?", (scan,)
    ).fetchone()
    return scan, int(row["n"])


def stamp_before(ctx: Context, span: timedelta) -> str:
    """The stored form of the instant `span` before now."""
    return ts(ctx.clock.now() - span)


def collect(ctx: Context) -> Collected:
    """Build the detector input. Store reads are plain SELECTs (A-8)."""
    now = stamp(ctx.clock)
    snapshots = _health(ctx)
    unreadable = _mail(ctx, snapshots)
    slots, unreadable_calendars = _calendars(ctx, snapshots)
    scan, orphans = _orphan_batch(ctx.db)
    links = card_links(ctx.db)
    data = DetectorInput(
        now=ctx.clock.now(),
        sources=snapshots,
        mail=tuple(repo.mail_batches(ctx.db)),
        mail_gaps=tuple(repo.mail_gaps(ctx.db)),
        sync_gaps=_sync_gaps(ctx),
        mail_held=tuple(repo.mail_held(ctx.db)),
        actions=tuple(due_actions(ctx.db, now)),
        cards=tuple(_cards_needing_review(ctx.db, now)),
        open_cards=tuple(
            memory_repo.card_rows(
                ctx.db.execute(
                    "SELECT handle, title, review_at, over_cap FROM cards WHERE state = 'open'"
                )
            )
        ),
        complex_decisions=tuple(_complex_decisions(ctx.db, stamp_before(ctx, NOTE_OPEN))),
        linked_ids=frozenset(links),
        linked_cards=links,
        grants=tuple(writes_repo.grants_awaiting_human(ctx.db)),
        review_grants=tuple(writes_repo.grants_for_review(ctx.db, now)),
        operations=tuple(
            writes_repo.uncertain_operations(ctx.db, stamp_before(ctx, UNSETTLED_AFTER))
        ),
        sends=tuple(writes_repo.rechecked_sends(ctx.db)),
        budgets=tuple(_over_budget(ctx.db)),
        orphan_batch=scan,
        orphan_count=orphans,
        runs_due=tuple(runs_repo.due_runs(ctx.db)),
        notes=tuple(runs_repo.open_notes(ctx.db, stamp_before(ctx, NOTE_OPEN))),
        calendar_slots=slots,
        update_tag=runs_repo.update_tag(ctx.db),
        reviews=tuple(review_repo.open_rounds(ctx.db)),
    )
    return Collected(data, frozenset(unreadable), frozenset(unreadable_calendars))
