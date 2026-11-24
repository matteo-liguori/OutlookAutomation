"""Lifetime rules of the runs track's own tables, run inside the sweeper's records step.

Contract: docs/contracts/06-memory.md (lifetime table; M-3, M-32). Reports are artifacts and
follow memory's rules; only the rows that name them are trimmed here.
"""

from datetime import datetime

from chamberlain.app.context import Context
from chamberlain.runs import local_events, repo
from chamberlain.runs.tuning import NOTE_KEEP, RUN_KEEP
from chamberlain.store.db import transaction


def trim_runs(ctx: Context, now: datetime) -> int:
    """Delete run rows 35 days past their day."""
    edge = (now - RUN_KEEP).date().isoformat()
    with transaction(ctx.db):
        return repo.delete_runs_before(ctx.db, edge)


def trim_notes(ctx: Context, now: datetime) -> int:
    """Delete notes 14 days after they were left."""
    edge = (now - NOTE_KEEP).strftime("%Y-%m-%dT%H:%M:%SZ")
    with transaction(ctx.db):
        return repo.delete_notes_before(ctx.db, edge)


COMPACTORS = [
    ("runs_trimmed", trim_runs),
    ("notes_trimmed", trim_notes),
    ("local_events_trimmed", local_events.trim),
]
"""Added to `app.wiring.COMPACTORS`."""
