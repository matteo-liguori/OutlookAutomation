"""Lifetime rules of the review track's own tables, run inside the sweeper's records step.

Contract: docs/contracts/06-memory.md (lifetime table; M-3, M-40).
"""

from datetime import datetime

from chamberlain.app.context import Context
from chamberlain.review import repo
from chamberlain.review.tuning import FILE_KEEP, PATTERN_KEEP, ROUND_KEEP
from chamberlain.store.db import transaction

_FORM = "%Y-%m-%dT%H:%M:%SZ"


def prune(ctx: Context, now: datetime) -> int:
    """Drop old patterns (and any over the cap), transcript positions, and finished rounds."""
    with transaction(ctx.db):
        return (
            repo.delete_patterns(ctx.db, (now - PATTERN_KEEP).strftime(_FORM))
            + repo.delete_files_before(ctx.db, (now - FILE_KEEP).strftime(_FORM))
            + repo.delete_rounds_before(ctx.db, (now - ROUND_KEEP).strftime(_FORM))
        )


COMPACTORS = [("review_pruned", prune)]
"""Added to `app.wiring.COMPACTORS`."""
