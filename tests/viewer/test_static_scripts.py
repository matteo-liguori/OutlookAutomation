"""Execute the viewer scripts and check their syntax with Node."""

import shutil
import subprocess
from pathlib import Path

import pytest

import chamberlain.surface.viewer as viewer_package

STATIC = Path(viewer_package.__file__).parent / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_viewer_scripts_parse_and_acknowledgement_requires_a_continuous_hold() -> None:
    node = shutil.which("node")
    assert node is not None
    for name in [
        "page.js",
        "calendar.js",
        "report.js",
        "appointments.js",
        "complex.js",
        "livetest.js",
    ]:
        done = subprocess.run(
            [node, "--check", str(STATIC / name)], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, (name, done.stderr)

    script = Path(__file__).with_name("appointments.test.mjs")
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=5, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_token_survives_a_reload_in_the_tab_only() -> None:
    """The token is kept in sessionStorage (this tab), never localStorage, and the fragment is
    still removed from the address bar."""
    script = (STATIC / "page.js").read_text(encoding="utf-8")
    assert "sessionStorage" in script
    assert "localStorage" not in script
    assert "history.replaceState" in script
    assert "try {" in script.split("function rememberedToken")[1].split("function rememberToken")[0]


def test_w41_unsettled_writes_are_only_shown_in_the_timeline_without_controls() -> None:
    script = (STATIC / "page.js").read_text(encoding="utf-8")
    needs = script.split("function needsView")[1].split("function grantsView")[0]
    timeline = script.split("function timelineView")[1].split("function runsView")[0]
    unsettled = script.split("function uncertainCard(")[1].split("function uncertainCards(")[0]
    assert "uncertainCards" not in needs
    assert "uncertainCards(box)" in timeline
    assert 'el("button"' not in unsettled
    assert "/resolve" not in script
