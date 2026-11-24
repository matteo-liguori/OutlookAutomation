"""The viewer's scripts are valid JavaScript (checked with Node when it is installed)."""

import shutil
import subprocess
from pathlib import Path

import pytest

import chamberlain.surface.viewer as viewer_package

STATIC = Path(viewer_package.__file__).parent / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
@pytest.mark.parametrize("name", ["page.js", "calendar.js", "livetest.js"])
def test_each_viewer_script_parses(name: str) -> None:
    node = shutil.which("node")
    assert node is not None
    done = subprocess.run(
        [node, "--check", str(STATIC / name)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
