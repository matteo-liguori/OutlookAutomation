"""Collect what detectors look at: store snapshots and bounded read-only source deltas.

Contract: docs/contracts/07-attention.md (A-6, A-7, A-8) and 08-ports.md (P-3, P-5).
A source that raises is logged and becomes a coverage gap, never a failed call.
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from chamberlain.app.context import Context
from chamberlain.attention import repo
from chamberlain.attention.models import (
    DetectorInput,
    MailDelta,
    NewMail,
    SourceSnapshot,
)
from chamberlain.attention.tuning import MAIL_LOOKBACK, MAIL_PAGE_LIMIT, NOTE_OPEN
from chamberlain.domain.capabilities import HealthState, SourceHealth
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.mail import FolderRole, MailQuery
from chamberlain.store.migrate import stamp

log = logging.getLogger("chamberlain.attention")

_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg)\s*:\s*)+", re.IGNORECASE)


def normalise_subject(subject: str) -> str:
    return " ".join(_PREFIX.sub("", subject).casefold().split())


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
        log.exception("health check failed for %s", source)
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


def _mail(ctx: Context, snapshots: tuple[SourceSnapshot, ...]) -> tuple[list[MailDelta], set[str]]:
    deltas: list[MailDelta] = []
    unreadable: set[str] = set()
    states = {s.id: s.state for s in snapshots}
    since = ctx.clock.now() - MAIL_LOOKBACK
    for source in sorted(ctx.sources.mail):
        if states.get(source) in (HealthState.UNAVAILABLE, HealthState.SIGN_IN_NEEDED):
            unreadable.add(source)
            continue
        try:
            port = ctx.sources.mail_port(source)
            inbox = [f for f in port.folders(source) if f.role is FolderRole.INBOX]
            if not inbox:
                unreadable.add(source)
                continue
            page = port.search(
                source,
                MailQuery(folder_id=inbox[0].id, unread=True, since=since, limit=MAIL_PAGE_LIMIT),
            )
            items: list[NewMail] = []
            for summary in page.items:
                handle = ctx.services.refs.remember(
                    HandleKind.MESSAGE, source, summary.locator, summary.internet_message_id
                )
                items.append(
                    NewMail(
                        handle,
                        source,
                        summary.internet_message_id,
                        normalise_subject(summary.subject),
                    )
                )
            complete = page.complete and states.get(source) is HealthState.OK
            if not complete:
                unreadable.add(source)
            deltas.append(MailDelta(source, tuple(items), complete))
        except Exception:
            log.exception("mail delta failed for %s", source)
            unreadable.add(source)
    return deltas, unreadable


def stamp_before(ctx: Context, span: timedelta) -> str:
    """The stored form of the instant `span` before now."""
    return (ctx.clock.now() - span).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(ctx: Context) -> Collected:
    """Build the detector input. Store reads are plain SELECTs (A-8)."""
    now = stamp(ctx.clock)
    snapshots = _health(ctx)
    deltas, unreadable = _mail(ctx, snapshots)
    scan, orphans = repo.orphan_batch(ctx.db)
    links = repo.card_links(ctx.db)
    data = DetectorInput(
        now=ctx.clock.now(),
        sources=snapshots,
        mail=tuple(deltas),
        actions=tuple(repo.due_actions(ctx.db, now)),
        cards=tuple(repo.cards_needing_review(ctx.db, now)),
        open_cards=tuple(repo.open_cards(ctx.db)),
        linked_ids=frozenset(links),
        linked_cards=links,
        grants=tuple(repo.grants_awaiting_human(ctx.db)),
        review_grants=tuple(repo.grants_for_review(ctx.db, now)),
        operations=tuple(repo.uncertain_operations(ctx.db)),
        budgets=tuple(repo.over_budget(ctx.db)),
        orphan_batch=scan,
        orphan_count=orphans,
        runs_due=tuple(repo.due_runs(ctx.db)),
        notes=tuple(repo.open_notes(ctx.db, stamp_before(ctx, NOTE_OPEN))),
        update_tag=repo.update_tag(ctx.db),
    )
    return Collected(data, frozenset(unreadable))
