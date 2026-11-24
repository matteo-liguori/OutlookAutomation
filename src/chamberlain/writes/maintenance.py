"""What the tick does for writes: recovery, grant timers, compaction, audit pruning.

Contract: docs/contracts/04-writes.md (W-9, W-25), 05-grants.md (G-2, G-8, G-22).
Attention's tick calls `maintain(ctx)`; it never raises into a tool call.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from chamberlain.app.context import Context
from chamberlain.domain.grant_rules import format_ts
from chamberlain.limits import AUDIT_DAYS, OPERATION_COMPACT_DAYS, UNCERTAIN_ABANDON_DAYS
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo
from chamberlain.writes.audit import AuditChain, prune
from chamberlain.writes.engine import Engine
from chamberlain.writes.models import StoredReceipt, WritePolicy


@dataclass(frozen=True)
class MaintenanceReport:
    recovered: int = 0
    grants_changed: int = 0
    operations_compacted: int = 0
    rows_removed: int = 0
    audit_pruned: int = 0
    abandoned: int = 0


def compact_operations(ctx: Context, now: datetime | None = None) -> int:
    """Replace settled operations older than 30 days with a one-line receipt (W-25)."""
    cutoff = format_ts((now or ctx.clock.now()) - timedelta(days=OPERATION_COMPACT_DAYS))
    count = 0
    with transaction(ctx.db):
        for op in repo.compactable_operations(ctx.db, cutoff):
            if op.receipt_json is None:
                continue
            summary = StoredReceipt.model_validate_json(op.receipt_json).summary
            line = StoredReceipt(summary=summary).model_dump_json(exclude_none=True)
            repo.compact_operation(ctx.db, op.handle, stamp(ctx.clock), line)
            count += 1
    return count


def abandon_stale(ctx: Context, policy: WritePolicy | None = None) -> int:
    """Close unsettled operations nobody can resend after 14 days. Frees nothing."""
    cutoff = format_ts(ctx.clock.now() - timedelta(days=UNCERTAIN_ABANDON_DAYS))
    engine = Engine(ctx, policy)
    closed = 0
    for op in repo.stale_uncertain(ctx.db, cutoff):
        closed += 1 if engine.abandon(op) is not None else 0
    return closed


def maintain(ctx: Context, policy: WritePolicy | None = None) -> MaintenanceReport:
    """One pass: crash recovery, grant timers, compaction, then the audit window."""
    engine = Engine(ctx, policy)
    recovered = engine.recover()
    changed = engine.grants.sweep()
    compacted = compact_operations(ctx)
    abandoned = abandon_stale(ctx, policy)
    before = format_ts(ctx.clock.now() - timedelta(days=AUDIT_DAYS))
    with transaction(ctx.db):
        removed = repo.delete_compacted_operations(ctx.db, before)
        removed += repo.delete_compacted_grants(ctx.db, before)
    pruned = prune(ctx.db, ctx.clock, AuditChain(ctx.db, ctx.clock))
    return MaintenanceReport(recovered, changed, compacted, removed, pruned, abandoned)


def recover_operations(ctx: Context) -> None:
    """The tick's crash-recovery step (W-9): expired leases become `uncertain`."""
    Engine(ctx).recover()


def compact_grants(ctx: Context, now: datetime) -> int:
    """Grant timers and compaction (G-2, G-8, G-9) for the sweeper's `compactors` seam."""
    return Engine(ctx).grants.sweep()


COMPACTORS: list[tuple[str, Callable[[Context, datetime], int]]] = [
    ("operations_compacted", compact_operations),
    ("grants_changed", compact_grants),
]
"""Pass to `memory.sweeper.sweep(..., compactors=COMPACTORS)`; each applies writes' own rules."""
