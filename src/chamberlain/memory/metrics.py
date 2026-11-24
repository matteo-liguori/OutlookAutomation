"""The storage figures of `doctor --metrics` that memory supplies.

Contract: docs/contracts/10-targets.md (`doctor --metrics` output). Keys owned
by other tracks (replies, guidance, operations, grants) stay at their defaults
here; the app merges the tracks' figures.
"""

import sqlite3
from pathlib import Path

from chamberlain.domain.cards import card_size
from chamberlain.memory import repo
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.memory.cards import CardService
from chamberlain.ports.clock import Clock
from chamberlain.schema.tools import Metrics
from chamberlain.store.db import DB_NAME


def tree_bytes(root: Path) -> int:
    total = 0
    if root.exists():
        for entry in root.rglob("*"):
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
    return total


def memory_metrics(db: sqlite3.Connection, clock: Clock, data_root: Path) -> Metrics:
    cards = CardService(db, clock)
    handles = [
        str(row["handle"]) for row in db.execute("SELECT handle FROM cards WHERE state = 'open'")
    ]
    sizes = [card_size(cards.view(handle)) for handle in handles]
    evidence_rows = repo.scalar(db, "SELECT count(*) FROM evidence")
    edges = ArtifactService(db, clock, data_root).edges()
    without_owner = repo.count_artifacts_without_live_owner(db, edges)
    db_file = data_root / DB_NAME
    return Metrics(
        open_cards=len(handles),
        open_card_chars_avg=sum(sizes) // len(sizes) if sizes else 0,
        open_card_chars_max=max(sizes, default=0),
        evidence_rows=evidence_rows,
        evidence_per_open_card=round(evidence_rows / len(handles), 2) if handles else 0.0,
        artifacts_live=repo.scalar(db, "SELECT count(*) FROM artifacts WHERE state = 'live'"),
        artifact_bytes=repo.total_stored_bytes(db),
        artifacts_without_owner=without_owner,
        orphans_quarantined=len(repo.held_quarantine(db)),
        db_bytes=db_file.stat().st_size if db_file.exists() else 0,
        data_root_bytes=tree_bytes(data_root),
    )
