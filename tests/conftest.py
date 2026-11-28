"""Shared fixtures for the local test suite."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from chamberlain.app.context import Context, Services, SourceRouter
from chamberlain.connectors.fake import FakeProvider
from chamberlain.ports.clock import ManualClock
from chamberlain.store.db import MEMORY, connect
from chamberlain.store.migrate import migrate
from chamberlain.writes.models import WritePolicy


@pytest.fixture(autouse=True)
def _agent_home_is_empty(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No test may read the real `~/.claude` or `~/.codex` (the review track reads transcripts).

    Every path to them goes through `chamberlain.review.homes`, which honours this variable.
    """
    monkeypatch.setenv("CHAMBERLAIN_AGENT_HOME", str(tmp_path_factory.mktemp("agent_home")))


@pytest.fixture
def clock() -> ManualClock:
    """A clock that moves only when a test moves it."""
    return ManualClock()


@pytest.fixture
def db(clock: ManualClock) -> Iterator[sqlite3.Connection]:
    """An in-memory store with every migration applied."""
    connection = connect(MEMORY)
    migrate(connection, clock, backup_dir=None)
    yield connection
    connection.close()


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """A throwaway data root. Tests never touch a real one (S-18)."""
    return tmp_path / "data"


@pytest.fixture
def fake(clock: ManualClock) -> FakeProvider:
    """A fake provider with one source of each kind, sharing the test clock."""
    provider = FakeProvider(clock)
    provider.mail.add_source("mail:test")
    provider.calendar.add_source("calendar:test")
    provider.contacts.add_source("contacts:test")
    return provider


@pytest.fixture
def ctx(db: sqlite3.Connection, clock: ManualClock, data_root: Path, fake: FakeProvider) -> Context:
    """A context over the in-memory store, the fake provider and real services."""
    sources = SourceRouter(
        mail={"mail:test": fake.mail},
        calendar={"calendar:test": fake.calendar},
        contacts={"contacts:test": fake.contacts},
    )
    return Context(
        db=db,
        clock=clock,
        data_root=data_root,
        sources=sources,
        services=Services.bind(db, clock, data_root, policy=WritePolicy()),
    )


@pytest.fixture(autouse=True)
def _no_real_browser_window(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No test may launch a real Edge or Chrome app window: the viewer falls back to
    `webbrowser.open`, which tests fake themselves."""
    from chamberlain.surface.viewer import presence

    monkeypatch.setattr(presence, "installed_browser", lambda: None)
    monkeypatch.setattr(presence, "close_profile_windows", lambda data_root, **_: None)
