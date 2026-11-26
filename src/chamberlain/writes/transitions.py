"""The one place an operation changes state (W-6, W-7).

Every transition is checked against `domain.operations.TRANSITIONS`, applied
as a compare-and-set, and written with its audit row in one transaction. A
terminal transition without a receipt is a defect and raises before touching
the store (every terminal operation has a receipt).
"""

import sqlite3

from chamberlain.domain.operations import (
    TERMINAL_STATES,
    OperationState,
    ProviderReceipt,
    Receipt,
    Verification,
    check_transition,
)
from chamberlain.domain.timefmt import parse
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo
from chamberlain.writes.audit import AuditChain
from chamberlain.writes.models import OperationRow, StoredReceipt


def move_operation(
    db: sqlite3.Connection,
    clock: Clock,
    audit: AuditChain,
    op: OperationRow,
    to: OperationState,
    *,
    actor: str,
    summary: str,
    expected_lease: str | bool | None = False,
    **columns: str | int | None,
) -> OperationRow | None:
    """Move `op` to `to`. Returns the new row, or None if someone else moved it first."""
    check_transition(op.state, to)
    if to in TERMINAL_STATES and "receipt_json" not in columns and op.receipt_json is None:
        raise ChamberlainError(
            ErrorCode.INTERNAL_UNEXPECTED, "A terminal operation needs a receipt (W-16)."
        )
    now = stamp(clock)
    if to in TERMINAL_STATES:
        columns.setdefault("settled_at", now)
    with transaction(db):
        applied = repo.update_operation(
            db,
            op.handle,
            expected_state=op.state,
            expected_lease=expected_lease,
            state=to.value,
            updated_at=now,
            **columns,
        )
        if not applied:
            return None
        audit.append(
            actor=actor,
            kind=f"operation.{to.value}",
            subject=op.handle,
            summary=summary,
            payload_sha256=op.intent_sha256,
        )
    return repo.get_operation(db, op.handle)


def build_receipt(
    op: OperationRow,
    state: OperationState,
    now: str,
    *,
    provider: ProviderReceipt | None = None,
    result_ref: str | None = None,
    reported: str | None = None,
) -> str:
    """The stored receipt of a terminal state (S-2: one named model)."""
    items = provider.items if provider else ()
    summary = Receipt(
        intent_kind=op.kind,
        source=op.source_id,
        state=state,
        intent_sha256=op.intent_sha256,
        settled_at=parse(now),
        verification=provider.verification if provider else Verification(),
        items_ok=sum(1 for item in items if item.ok),
        items_failed=sum(1 for item in items if not item.ok),
    )
    stored = StoredReceipt(
        summary=summary,
        result_ref=result_ref,
        items=items,
        warnings=provider.warnings if provider else (),
        reported=reported,
    )
    return stored.model_dump_json(exclude_none=True)
