"""The viewer's scripts are valid JavaScript (checked with Node when it is installed)."""

import shutil
import subprocess
from pathlib import Path

import pytest

import chamberlain.surface.viewer as viewer_package

STATIC = Path(viewer_package.__file__).parent / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
@pytest.mark.parametrize(
    "name", ["page.js", "calendar.js", "report.js", "appointments.js", "complex.js", "livetest.js"]
)
def test_each_viewer_script_parses(name: str) -> None:
    node = shutil.which("node")
    assert node is not None
    done = subprocess.run(
        [node, "--check", str(STATIC / name)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_the_token_survives_a_reload_in_the_tab_only() -> None:
    """The token is kept in sessionStorage (this tab), never localStorage, and the fragment is
    still removed from the address bar."""
    script = (STATIC / "page.js").read_text(encoding="utf-8")
    assert "sessionStorage" in script
    assert "localStorage" not in script
    assert "history.replaceState" in script
    assert "try {" in script.split("function rememberedToken")[1].split("function rememberToken")[0]
