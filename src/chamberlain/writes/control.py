"""Withdrawing operations and ending grants on request (T-11, `WriteControl`).

`forget` reaches grants and operations through `app.services.WriteControl`.
A grant is revoked (a status change, G-2); an operation is cancelled only
before it is prepared, and a dispatched one can never be touched here.
"""

import logging
import sqlite3
from collections.abc import Callable

from pydantic import TypeAdapter

from chamberlain.app.services import ArtifactStore, AuditLog
from chamberlain.domain.intents import WriteIntent
from chamberlain.domain.operations import (
    MAY_HAVE_HAPPENED_STATES,
    TERMINAL_STATES,
    OperationState,
)
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo
from chamberlain.writes.grants import GrantService
from chamberlain.writes.models import OperationRow, WritePolicy
from chamberlain.writes.transitions import build_receipt, move_operation

log = logging.getLogger("chamberlain.writes")
_INTENT: TypeAdapter[WriteIntent] = TypeAdapter(WriteIntent)


def release_holds(artifacts: ArtifactStore | None, op: OperationRow) -> None:
    """Let go of the attachments an operation held (M-11)."""
    if artifacts is None or op.intent_json is None:
        return
    intent = _INTENT.validate_json(op.intent_json)
    for attachment in getattr(intent, "attachments", ()):
        try:
            artifacts.release_owner(attachment.artifact, op.handle)
        except ChamberlainError:
            log.warning("could not release an attachment hold for %s", op.handle)


def cancel_operation(
    db: sqlite3.Connection,
    clock: Clock,
    audit: AuditLog,
    grants: GrantService,
    handle: str,
    *,
    actor: str,
    release: Callable[[OperationRow], None],
) -> OperationRow:
    """`proposed | authorised -> cancelled`; a waiting proposal is withdrawn with it."""
    op = repo.get_operation(db, handle)
    if op is None:
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such operation.")
    if op.state in TERMINAL_STATES:
        return op
    if op.state in MAY_HAVE_HAPPENED_STATES:
        raise ChamberlainError(
            ErrorCode.OPERATION_NEEDS_RECONCILE,
            f"Operation {handle} may have happened. Call reconcile; never send again.",
        )
    if op.state is OperationState.PREPARED:
        raise ChamberlainError(
            ErrorCode.REQUEST_WRONG_KIND, "That operation is already prepared; call do."
        )
    moved = move_operation(
        db,
        clock,
        audit,
        op,
        OperationState.CANCELLED,
        actor=actor,
        summary="Withdrawn before dispatch.",
        receipt_json=build_receipt(op, OperationState.CANCELLED, stamp(clock)),
    )
    if moved is None:
        refreshed = repo.get_operation(db, handle)
        if refreshed is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such operation.")
        return refreshed
    waiting = repo.proposed_one_off(db, op.source_id, op.intent_sha256)
    if waiting is not None:
        grants.revoke(waiting.handle, actor=actor)
    release(moved)
    return moved


class Control:
    """The `WriteControl` service (T-11)."""

    def __init__(
        self,
        db: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        policy: WritePolicy,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._artifacts = artifacts
        self._grants = GrantService(db, clock, audit, policy)

    def revoke_grant(self, handle: str, *, actor: str) -> None:
        self._grants.revoke(handle, actor=actor)

    def cancel_operation(self, handle: str, *, actor: str) -> None:
        cancel_operation(
            self._db,
            self._clock,
            self._audit,
            self._grants,
            handle,
            actor=actor,
            release=lambda op: release_holds(self._artifacts, op),
        )
