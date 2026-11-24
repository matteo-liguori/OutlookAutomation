"""The cross-track services memory implements.

Contract: docs/contracts/09-storage.md and `app/services.py`. `build` returns
the three objects `app/wiring.py` puts into `Context.services`.
"""

import sqlite3
from pathlib import Path

from chamberlain.app.services import ArtifactStore, EvidenceStore, RefResolver
from chamberlain.memory.artifacts import ArtifactService, Budgets
from chamberlain.memory.evidence import EvidenceService
from chamberlain.memory.refs import RefService
from chamberlain.ports.clock import Clock


def build(
    db: sqlite3.Connection,
    clock: Clock,
    data_root: Path,
    budgets: Budgets | None = None,
) -> tuple[ArtifactStore, EvidenceStore, RefResolver]:
    """The `ArtifactStore`, `EvidenceStore` and `RefResolver` over one store."""
    return (
        ArtifactService(db, clock, data_root, budgets),
        EvidenceService(db, clock),
        RefService(db, clock),
    )
