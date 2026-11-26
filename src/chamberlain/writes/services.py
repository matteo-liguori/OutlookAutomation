"""The services the writes track gives the app (`AuditLog`, `WriteControl`).

Contract: docs/contracts/09-storage.md (cross-track needs go through
`app/services.py`), docs/contracts/02-tools.md (T-11).
"""

import sqlite3

from chamberlain.app.services import ArtifactStore, AuditLog, WriteControl
from chamberlain.ports.clock import Clock
from chamberlain.writes.audit import AuditChain
from chamberlain.writes.control import Control
from chamberlain.writes.models import WritePolicy


def build(
    db: sqlite3.Connection,
    clock: Clock,
    *,
    policy: WritePolicy,
    artifacts: ArtifactStore | None = None,
) -> tuple[AuditLog, WriteControl]:
    """The audit log and the write control, both bound to one connection.

    `artifacts` lets a cancelled operation let go of its attachment holds.
    """
    audit = AuditChain(db, clock)
    control = Control(db, clock, audit, policy, artifacts)
    return audit, control
