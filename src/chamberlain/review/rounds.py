"""Review rounds: when one opens, when it ends, and what the agent reads in it.

Contract: docs/contracts/06-memory.md (M-40), 07-attention.md (A-1c). How often depends on how
much is new (owner ruling 2026-11-24): a round opens once enough new patterns were logged since
the last round (but not within a few days of it), or after a long gap if anything new was logged.
It ends when the agent marks its `harness.review` item handled, or lapses after 14 days.
Attention raises the item from the open round; the event key is `harness.review:-:<round>`.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from chamberlain.app.context import Context
from chamberlain.review import repo
from chamberlain.review.tuning import BRIEF_ROWS, ROUND_LAPSE
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp


@dataclass(frozen=True)
class RoundRule:
    """When a review round opens; the profile's `review` section."""

    new_patterns: int = 5
    min_days: int = 3
    max_days: int = 60

    def due(self, fresh: int, age: timedelta) -> bool:
        if fresh <= 0:
            return False
        if age >= timedelta(days=self.max_days):
            return True
        return fresh >= self.new_patterns and age >= timedelta(days=self.min_days)


def event_key(round_id: int) -> str:
    return f"harness.review:-:{round_id}"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def refresh(ctx: Context, rule: RoundRule) -> None:
    """Close rounds that ended, then open one when it is time."""
    now = ctx.clock.now()
    stamped = stamp(ctx.clock)
    with transaction(ctx.db):
        for round_ in repo.open_rounds(ctx.db):
            if repo.event_state(ctx.db, event_key(round_.id)) == "handled":
                repo.close_round(ctx.db, round_.id, "done", stamped)
            elif now - _parse(round_.opened_at) > ROUND_LAPSE:
                repo.close_round(ctx.db, round_.id, "lapsed", stamped)
        if repo.open_rounds(ctx.db):
            return
        latest = repo.latest_round(ctx.db)
        baseline = latest.opened_at if latest else repo.first_added(ctx.db)
        if baseline is None:
            return
        fresh = repo.count_patterns_added_after(ctx.db, latest.opened_at if latest else None)
        if rule.due(fresh, now - _parse(baseline)):
            repo.insert_round(ctx.db, stamped, fresh)


def brief(ctx: Context) -> str:
    """What the agent reads: patterns, newest first, with counts. Tool names only."""
    open_now = repo.open_rounds(ctx.db)
    since = repo.previous_round_opened(ctx.db, open_now[-1].id) if open_now else None
    rows = repo.patterns(ctx.db, since, BRIEF_ROWS + 1)
    lines = [
        "What the agent ran outside the Chamberlain tools. Only the tool name and the first",
        "words of a command are kept, never what a command printed or what a file held.",
        "NEW: first logged since the last review. Columns: times, last seen, tool, command.",
        "",
    ]
    for row in rows[:BRIEF_ROWS]:
        new = "NEW " if since is None or row.added_at > since else ""
        command = f": {row.pattern}" if row.pattern else ""
        lines.append(
            f"{row.total:>5}  {row.last_seen_at[:10]}  {new}{row.agent} {row.tool}{command}"
        )
    if len(rows) > BRIEF_ROWS:
        lines.append(f"(more patterns exist; the {BRIEF_ROWS} shown are new or most used)")
    if not rows:
        lines.append("Nothing has been logged.")
    return "\n".join(lines)
