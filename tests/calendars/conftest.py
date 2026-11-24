"""Fixtures for the calendar connector tests. Fakes, Radicale on loopback and temp dirs only."""

import sys
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend

sys.path.insert(0, str(Path(__file__).parent))

from cal_radicale import RadicaleServer


class MemoryKeyring(KeyringBackend):
    """An in-memory credential store so tests never touch the real one."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("absent")
        del self.items[(service, username)]


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
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
