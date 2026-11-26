"""Doctor's check selectors and metrics through the wired tool (T-16)."""

from dataclasses import replace
from pathlib import Path

import pytest

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.app.wiring import app_handlers
from chamberlain.schema.tools import DoctorCheck, DoctorResponse


def test_t16_doctor_runs_six_check_groups_and_returns_sixteen_metrics(ctx: Context) -> None:
    ctx.data_root.mkdir()
    app = App(ctx, app_handlers())
    groups = {group.value for group in DoctorCheck if group is not DoctorCheck.ALL}
    assert groups == {"sources", "storage", "guidance", "audit", "schema", "access"}

    seen: set[str] = set()
    for group in groups:
        individual = app.call("doctor", {"check": group})
        assert individual.ok
        assert isinstance(individual.data, DoctorResponse)
        ids = [item.id for item in individual.data.checks]
        assert ids
        assert len(ids) == len(set(ids))
        assert seen.isdisjoint(ids)
        assert individual.data.metrics is None
        seen.update(ids)

    all_checks = app.call("doctor", {"check": "all", "metrics": True})
    assert all_checks.ok
    assert isinstance(all_checks.data, DoctorResponse)
    assert {item.id for item in all_checks.data.checks} == seen
    assert len(all_checks.data.checks) == len(seen)
    assert all_checks.data.metrics is not None
    metrics = all_checks.data.metrics.model_dump(mode="json")
    assert len(metrics) == 16
    assert metrics["guidance_total_chars"] > 0
    assert metrics["largest_reply_chars_7d"] > 0


@pytest.mark.parametrize("situation", ["readable", "checkout", "missing", "file"])
def test_t16_doctor_access_reports_the_private_data_root_without_writing(
    ctx: Context, situation: str
) -> None:
    root = ctx.data_root
    match situation:
        case "readable":
            root.mkdir()
        case "checkout":
            root = Path(__file__).resolve().parents[2] / "src"
        case "file":
            root.write_text("fixture", encoding="utf-8")
    expected = {
        "readable": ("ok", "can be listed"),
        "checkout": ("fail", "inside the code checkout"),
        "missing": ("fail", "could not list"),
        "file": ("fail", "could not list"),
    }
    app = App(replace(ctx, data_root=root), app_handlers())
    reply = app.call("doctor", {"check": "access"})
    assert reply.ok
    assert isinstance(reply.data, DoctorResponse)
    [check] = reply.data.checks
    status, text = expected[situation]
    assert check.id == "access.data_root"
    assert check.status == status
    assert text in check.summary
    if situation == "readable":
        assert not list(root.iterdir())
    elif situation == "missing":
        assert not root.exists()
    elif situation == "file":
        assert root.read_text(encoding="utf-8") == "fixture"
