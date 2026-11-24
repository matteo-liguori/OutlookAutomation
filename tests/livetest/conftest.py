"""Fixtures for the live-test self-test: the fake world, and the test playing the owner."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from chamberlain.app.context import Context
from chamberlain.livetest.authority import start_standing_proposals
from chamberlain.livetest.fakeworld import FakeLab, build_fake_lab
from chamberlain.livetest.runner import RunOptions, open_lab
from chamberlain.writes.confirm import confirm_grant


def confirm_all(lab: FakeLab, options: RunOptions | None = None) -> list[str]:
    """What the owner does in the viewer: open each proposed grant and confirm it.

    This is the test's own code, standing in for a person. The runner under test has no
    way to do it (`test_the_runner_cannot_confirm`).
    """
    chosen = options or RunOptions()
    live = open_lab(lab.app, lab.env, lab.config, "20260105T090000-0000", chosen)
    states = start_standing_proposals(live, list(lab.config.mail))
    handles: list[str] = []
    for state in states:
        if state.state == "proposed":
            ctx: Context = lab.app.context
            grant = lab.app.context.db.execute(
                "SELECT content_sha256 FROM grants WHERE handle = ?", (state.handle,)
            ).fetchone()
            confirm_grant(ctx, state.handle, {"content_sha256": grant["content_sha256"]})
            handles.append(state.handle)
    return handles


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[FakeLab]:
    built = build_fake_lab(tmp_path / "data")
    try:
        yield built
    finally:
        built.close()
