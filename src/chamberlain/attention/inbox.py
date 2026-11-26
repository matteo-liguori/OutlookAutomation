"""The event inbox: upsert by key, expire, handle, quiet periods, delivery.

Contract: docs/contracts/07-attention.md (A-1 to A-4).
"""

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta

from chamberlain.attention import repo
from chamberlain.attention.models import EventDraft, EventRow
from chamberlain.attention.tuning import RERAISE
from chamberlain.domain.events import NOT_AGENT_HANDLEABLE, EventKind
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp


def _suppressed(handled: EventRow, now: datetime) -> bool:
    """A handled event whose condition is still true stays quiet (A-3)."""
    if handled.quiet_until is None:
        return True
    return now < datetime.fromisoformat(handled.quiet_until)


def reconcile(
    db: sqlite3.Connection,
    clock: Clock,
    drafts: Iterable[EventDraft],
    *,
    evaluated: frozenset[EventKind],
    unreadable_sources: frozenset[str] = frozenset(),
    hold: frozenset[EventKind] = frozenset(),
) -> None:
    """Make the inbox match what the detectors report (A-1, A-2, A-3).

    Only kinds in `evaluated` are touched: a detector that failed leaves its
    events alone. Mail events of a source that could not be read are kept, and so are events of the
    kinds in `hold` (their data was not fully read, so silence proves nothing).
    """
    now = clock.now()
    stamped = stamp(clock)
    reported = {draft.dedupe_key: draft for draft in drafts if draft.kind in evaluated}
    with transaction(db):
        live = {event.dedupe_key: event for event in repo.live_events(db)}
        handled = repo.handled_by_key(db)
        for key, draft in sorted(reported.items()):
            quiet = handled.get(key)
            if quiet is not None:
                if _suppressed(quiet, now):
                    continue
                repo.delete_event(db, quiet.handle)
            existing = live.get(key)
            if existing is None:
                repo.insert_event(db, new_handle(HandleKind.EVENT), draft, stamped)
            elif (existing.summary, existing.priority, existing.attrs) != (
                draft.summary,
                draft.priority,
                draft.attrs,
            ):
                repo.refresh_event(db, existing.handle, draft)
        for key, event in live.items():
            if key in reported or event.kind not in evaluated or event.kind in hold:
                continue
            if (
                event.kind is EventKind.MAIL_NEW_UNMATCHED
                and event.attrs.source in unreadable_sources
            ):
                continue
            repo.expire_event(db, event.handle, stamped)
        for key, event in handled.items():
            if key not in reported and event.kind in evaluated:
                repo.delete_event(db, event.handle)
        repo.trim_terminal(db, _before(clock, days=14))


def _before(clock: Clock, *, days: int) -> str:
    return (clock.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def handle_events(db: sqlite3.Connection, clock: Clock, handles: Iterable[str]) -> list[str]:
    """Mark events handled on the agent's say-so (A-2, A-4); return those closed.

    Unknown handles raise before anything changes. Events that cannot be handled
    by the agent, and events already closed, are left as they are.
    """
    wanted = list(dict.fromkeys(handles))
    with transaction(db):
        found = {event.handle: event for event in repo.events_by_handles(db, wanted)}
        missing = [h for h in wanted if h not in found]
        if missing:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE,
                "One of the events is not known.",
                fix="List only event handles that start or next returned.",
            )
        closed: list[str] = []
        now = clock.now()
        for handle in wanted:
            event = found[handle]
            if event.kind in NOT_AGENT_HANDLEABLE or event.state.value not in ("new", "delivered"):
                continue
            interval = RERAISE.get(event.kind)
            quiet = None if interval is None else (now + interval).strftime("%Y-%m-%dT%H:%M:%SZ")
            repo.mark_handled(db, handle, stamp(clock), quiet)
            if event.kind is EventKind.MAIL_NEW_UNMATCHED and event.subject is not None:
                repo.triage_delivered(db, event.subject, handle, sent=event.attrs.reason == "sent")
            closed.append(handle)
        return closed


def deliver(db: sqlite3.Connection, clock: Clock, session: str, events: Iterable[EventRow]) -> None:
    """Record that this session was shown these events. Delivery never closes them (A-2)."""
    with transaction(db):
        for event in events:
            repo.mark_delivered(db, event.handle, session, stamp(clock))
            if event.kind is EventKind.MAIL_NEW_UNMATCHED and event.subject is not None:
                repo.stamp_delivered(
                    db,
                    event.subject,
                    event.handle,
                    sent=event.attrs.reason == "sent",
                    session=session,
                )
