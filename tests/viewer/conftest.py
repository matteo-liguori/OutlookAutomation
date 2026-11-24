"""Fixtures for the viewer tests: a real wired app, no sockets, no browser, no network."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from viewer_rig import Rig


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path / "data")
    try:
        yield made
    finally:
        made.close()
