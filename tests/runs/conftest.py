"""Fixtures for the runs tests.

The clock starts on a Monday, 2026-01-05 06:00 UTC. Attention's wrong-day guard reads the
operating system's zone (a board question), so the tests pin it to UTC to stay deterministic.
"""

import sys
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from runs_rig import Rig

from chamberlain.attention import sessions


@pytest.fixture(autouse=True)
def utc_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sessions, "_local_day", lambda moment, zone=None: moment.astimezone(UTC).date()
    )


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path / "data")
    try:
        yield made
    finally:
        made.app.close()
