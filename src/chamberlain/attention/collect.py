"""Collect what detectors look at: store snapshots and bounded read-only source deltas.

Contract: docs/contracts/07-attention.md (A-6, A-7, A-8) and 08-ports.md (P-3, P-5).
A source that raises is logged and becomes a coverage gap, never a failed call.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from chamberlain.app.context import Context
from chamberlain.attention import repo
from chamberlain.attention.models import (
    DetectorInput,
    HeldMail,
    MailGap,
    SourceSnapshot,
    SyncGap,
)
from chamberlain.attention.suggest import Suggester
from chamberlain.attention.tuning import NOTE_OPEN
from chamberlain.attention.watermark import scan
from chamberlain.domain.capabilities import HealthState, SourceHealth
from chamberlain.schema.safelog import log_failure
from chamberlain.store.migrate import stamp

log = logging.getLogger("chamberlain.attention")


@dataclass(frozen=True)
class Collected:
    data: DetectorInput
    unreadable: frozenset[str]
    """Sources whose mail could not be fully read this tick."""


def source_health(ctx: Context, source: str) -> SourceHealth | None:
    """Ask the owning port; None when the port raises (A-7)."""
    kind = source.split(":", 1)[0]
    try:
        if kind == "mail":
            return ctx.sources.mail_port(source).health(source)
        if kind == "calendar":
            return ctx.sources.calendar_port(source).health(source)
        return ctx.sources.contacts_port(source).health(source)
    except Exception:
        log_failure(log, "health check failed for %s", source)
        return None


def _health(ctx: Context) -> tuple[SourceSnapshot, ...]:
    snapshots: list[SourceSnapshot] = []
    for source in ctx.sources.source_ids():
        health = source_health(ctx, source)
        state = HealthState.UNAVAILABLE if health is None else health.state
        fresh = None if health is None or health.fresh_as_of is None else health.fresh_as_of
        repo.write_health(
            ctx.db,
            source,
            state,
            stamp(ctx.clock),
            None if fresh is None else fresh.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        snapshots.append(SourceSnapshot(source, state))
    return tuple(snapshots)


def _mail(ctx: Context, snapshots: tuple[SourceSnapshot, ...]) -> set[str]:
    """Scan every mail source from its watermark; return the sources not fully covered."""
    unreadable: set[str] = set()
    states = {s.id: s.state for s in snapshots}
    suggester = Suggester(ctx)
    for source in sorted(ctx.sources.mail):
        try:
            if not scan(ctx, source, states.get(source), suggester).complete:
                unreadable.add(source)
        except Exception:
            log_failure(log, "mail scan failed for %s", source)
            unreadable.add(source)
    return unreadable


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


def stamp_before(ctx: Context, span: timedelta) -> str:
    """The stored form of the instant `span` before now."""
    return (ctx.clock.now() - span).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(ctx: Context) -> Collected:
    """Build the detector input. Store reads are plain SELECTs (A-8)."""
    now = stamp(ctx.clock)
    snapshots = _health(ctx)
    unreadable = _mail(ctx, snapshots)
    scan, orphans = repo.orphan_batch(ctx.db)
    links = repo.card_links(ctx.db)
    data = DetectorInput(
        now=ctx.clock.now(),
        sources=snapshots,
        mail=tuple(repo.mail_batches(ctx.db)),
        mail_gaps=tuple(MailGap(*g) for g in repo.mail_gaps(ctx.db)),
        sync_gaps=_sync_gaps(ctx),
        mail_held=tuple(
            HeldMail(source, h.start, h.end, h.state, h.estimate)
            for source, h in repo.mail_held(ctx.db)
        ),
        actions=tuple(repo.due_actions(ctx.db, now)),
        cards=tuple(repo.cards_needing_review(ctx.db, now)),
        open_cards=tuple(repo.open_cards(ctx.db)),
        complex_decisions=tuple(repo.complex_decisions(ctx.db, stamp_before(ctx, NOTE_OPEN))),
        linked_ids=frozenset(links),
        linked_cards=links,
        grants=tuple(repo.grants_awaiting_human(ctx.db)),
        review_grants=tuple(repo.grants_for_review(ctx.db, now)),
        operations=tuple(repo.uncertain_operations(ctx.db)),
        sends=tuple(repo.rechecked_sends(ctx.db)),
        budgets=tuple(repo.over_budget(ctx.db)),
        orphan_batch=scan,
        orphan_count=orphans,
        runs_due=tuple(repo.due_runs(ctx.db)),
        notes=tuple(repo.open_notes(ctx.db, stamp_before(ctx, NOTE_OPEN))),
        update_tag=repo.update_tag(ctx.db),
    )
    return Collected(data, frozenset(unreadable))
