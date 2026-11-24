"""Fixtures for the importer tests: a synthetic old root, a fresh data root, a fake secret store."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from old_fixture import MemorySecrets, build_old_root

from chamberlain.importer.report import ImportReport
from chamberlain.importer.run import run_import
from chamberlain.ports.clock import ManualClock


@pytest.fixture
def old_root(tmp_path: Path) -> Path:
    return build_old_root(tmp_path / "old")


@pytest.fixture
def secrets() -> MemorySecrets:
    return MemorySecrets()


@pytest.fixture
def imported(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> ImportReport:
    return run_import(old_root, data_root, clock=clock, secrets=secrets)
