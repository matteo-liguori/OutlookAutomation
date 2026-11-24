"""Fixtures for the importer tests: a synthetic old root, a fresh data root, a fake secret store."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from old_fixture import MemorySecrets, build_old_root


@pytest.fixture
def old_root(tmp_path: Path) -> Path:
    return build_old_root(tmp_path / "old")


@pytest.fixture
def secrets() -> MemorySecrets:
    return MemorySecrets()
