"""Fixtures for the Thunderbird connector tests."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from thunderbird_rig import Stack, node_available, running


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    """A connector wired to the real add-on under Node. Missing Node is a loud failure."""
    if not node_available():
        if os.environ.get("CHAMBERLAIN_ALLOW_NO_NODE") == "1":
            pytest.skip("node is not installed (CHAMBERLAIN_ALLOW_NO_NODE=1)")
        pytest.fail("Node.js is required for the Thunderbird gate tests (install Node 22 or newer)")
    with running(tmp_path) as live:
        yield live
