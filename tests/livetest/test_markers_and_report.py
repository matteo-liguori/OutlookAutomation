"""The marker rules, the score, and the reports (written, read, trimmed, free of text)."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chamberlain.livetest.markers import MARKER, carries_marker, new_run_id, run_marker, run_of
from chamberlain.livetest.model import RunReport, ScenarioResult, Status, compute_score
from chamberlain.livetest.report import (
    markdown,
    read_reports,
    report_dir,
    table,
    trim_reports,
    write_report,
)

RUN = "20261124T153000-ab12"


def test_only_a_subject_that_opens_with_the_marker_carries_it() -> None:
    for subject, expected in [
        (f"{MARKER} {RUN} self_send", True),
        (f"RE: Fwd: {MARKER} {RUN} x", True),
        (f"Hello {MARKER} {RUN}", False),
        (f"{MARKER} notes", False),
        (f"{MARKER.lower()} {RUN}", False),
        ("", False),
        # A calendar's own emails about a marked event (found live, 2026-11-25).
        (f"Accepted: {MARKER} {RUN} invite test @ Sun 29 Nov 2026 10am", True),
        (f"Invitation: {MARKER} {RUN} invite @ Sun 29 Nov", True),
        (f"Matteo Liguori invited you to “{MARKER} {RUN} invite icloud”", True),
        (f"“{MARKER} {RUN} invite icloud” has been cancelled by Matteo.", True),
        # Still never a message that merely mentions it.
        (f"Accepted: Lunch about {MARKER} {RUN}", False),
        (f"Someone invited you to lunch, see {MARKER} {RUN}", False),
    ]:
        assert carries_marker(subject) is expected, subject
    first = new_run_id(datetime(2026, 11, 24, 15, 30, tzinfo=UTC), "ab12ffff")
    assert first == RUN
    assert run_of(f"{run_marker(first)} self_send x") == first


def result(id_: str, status: Status, reason: str = "") -> ScenarioResult:
    return ScenarioResult(id=id_, group="mail", status=status, reason=reason, seconds=1.5)


def report(results: tuple[ScenarioResult, ...], leftovers: tuple[str, ...] = ()) -> RunReport:
    return RunReport(
        run_id=RUN,
        started_at="2026-11-24T15:30:00Z",
        finished_at="2026-11-24T15:35:00Z",
        mode="fake",
        sources=("mail:a",),
        results=results,
        score=compute_score(results, len(leftovers)),
        leftovers=leftovers,
    )


def test_the_score_counts_passes_over_runs_leftovers_as_failures_and_skips_as_coverage() -> None:
    results = (
        result("a", Status.PASS),
        result("b", Status.PASS),
        result("c", Status.FAIL, "broke"),
        result("d", Status.SKIPPED, "no source"),
    )
    plain = compute_score(results, 0)
    assert (plain.percent, plain.coverage_percent) == (67, 75)
    assert compute_score(results, 1).percent == 50
    nothing = compute_score((result("a", Status.SKIPPED, "off"),), 0)
    assert (nothing.percent, nothing.coverage_percent) == (100, 0)


def test_reports_are_written_read_back_trimmed_and_hold_no_text(tmp_path: Path) -> None:
    done = report(
        (result("a", Status.PASS), result("b", Status.FAIL, "broke")), ("msg_aaaaaaaaaaaa",)
    )
    now = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    json_path, md_path = write_report(tmp_path, done, now)
    assert json_path == report_dir(tmp_path) / f"{RUN}.json"
    assert md_path.read_text(encoding="utf-8").startswith(f"# Live test {RUN}")
    assert read_reports(tmp_path) == [done]
    text = done.model_dump_json() + markdown(done) + table(done)
    assert MARKER not in text
    assert "@" not in text
    assert "Score 33%: 1 passed, 2 failed" in table(done)
    assert any(line.startswith("  FAIL  b") for line in table(done).splitlines())

    folder = report_dir(tmp_path)
    old = folder / "20260101T000000-0000.json"
    old.write_text("{}", encoding="utf-8")
    stamp = (now - timedelta(days=30)).timestamp()
    os.utime(old, (stamp, stamp))
    (folder / "20261124T150000-0000.json").write_text("not json", encoding="utf-8")
    (folder / "notes.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    assert trim_reports(tmp_path, now) == 1
    assert not old.exists()
    assert read_reports(tmp_path) == [done]  # unreadable and foreign files are skipped
