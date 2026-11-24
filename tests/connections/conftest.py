"""Fixtures for the connection profile tests."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from conn_support import MemorySecrets


@pytest.fixture
def secrets() -> MemorySecrets:
    return MemorySecrets()
