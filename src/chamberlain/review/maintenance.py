"""Lifetime rules of the review track's own tables, run inside the sweeper's records step.

Contract: docs/contracts/06-memory.md (lifetime table; M-3, M-40).
"""

from datetime import datetime

from chamberlain.app.context import Context
from chamberlain.domain.timefmt import ts
from chamberlain.review import repo
from chamberlain.review.tuning import FILE_KEEP, PATTERN_KEEP, ROUND_KEEP
from chamberlain.store.db import transaction


def prune(ctx: Context, now: datetime) -> int:
    """Drop old patterns (and any over the cap), transcript positions, and finished rounds."""
    with transaction(ctx.db):
        return (
            repo.delete_patterns(ctx.db, ts(now - PATTERN_KEEP))
            + repo.delete_files_before(ctx.db, ts(now - FILE_KEEP))
            + repo.delete_rounds_before(ctx.db, ts(now - ROUND_KEEP))
        )


COMPACTORS = [("review_pruned", prune)]
"""Added to `app.wiring.COMPACTORS`."""
