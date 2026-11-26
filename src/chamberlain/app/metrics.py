"""`doctor --metrics`: every fixed key of docs/contracts/10-targets.md.

Memory supplies the storage figures (`memory.metrics`); the rest are plain reads of
other tracks' tables (S-3) and of the audit log. Nothing here writes.
"""

import re
from datetime import timedelta

from chamberlain.app.context import Context
from chamberlain.app.core import REPLY_SIZE_PATTERN
from chamberlain.attention.overrides import apply_overrides
from chamberlain.attention.rules import load_rules
from chamberlain.domain.grant_rules import format_ts
from chamberlain.memory.metrics import memory_metrics
from chamberlain.schema.tools import Metrics
from chamberlain.writes.repo import permission_metric_counts, tool_call_summaries

_SIZE = re.compile(REPLY_SIZE_PATTERN)
REPLY_WINDOW_DAYS = 7


def largest_reply_chars(ctx: Context) -> int:
    """The largest reply any tool call produced in the last seven days (audit rows)."""
    since = format_ts(ctx.clock.now() - timedelta(days=REPLY_WINDOW_DAYS))
    largest = 0
    for summary in tool_call_summaries(ctx.db, since):
        found = _SIZE.match(summary)
        if found:
            largest = max(largest, int(found.group(1)))
    return largest


def guidance_total_chars(ctx: Context) -> int:
    """The text of every rule in force, owner patches applied."""
    applied = apply_overrides(load_rules(), ctx.data_root)
    return sum(len(rule.text) for rule in applied.rules)


def collect_metrics(ctx: Context) -> Metrics:
    """All sixteen keys, from one read-only pass."""
    stored = memory_metrics(ctx.db, ctx.clock, ctx.data_root)
    uncertain, active, waiting = permission_metric_counts(ctx.db)
    return stored.model_copy(
        update={
            "largest_reply_chars_7d": largest_reply_chars(ctx),
            "guidance_total_chars": guidance_total_chars(ctx),
            "uncertain_operations": uncertain,
            "grants_active": active,
            "grants_awaiting_human": waiting,
        }
    )
