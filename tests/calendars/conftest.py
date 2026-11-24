"""Fixtures for the calendar connector tests. Fakes, Radicale on loopback and temp dirs only."""

import sys
from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cal_keyring import MemoryKeyring
from cal_radicale import RadicaleServer


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    """Every test here gets an in-memory credential store, never the real vault."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(scope="session")
def radicale(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RadicaleServer]:
    server = RadicaleServer(tmp_path_factory.mktemp("radicale"))
    yield server
    server.stop()
