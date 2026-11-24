"""The marker rules, the score, and the reports (written, read, trimmed, free of text)."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chamberlain.livetest.markers import (
    MARKER,
    carries_marker,
    new_run_id,
    run_marker,
    run_of,
    subject_for,
)
from chamberlain.livetest.model import (
    RunReport,
    ScenarioResult,
    Status,
    compute_score,
)
from chamberlain.livetest.report import (
    markdown,
    read_reports,
    report_dir,
    table,
    trim_reports,
    write_report,
)

RUN = "20261124T153000-ab12"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        (f"{MARKER} {RUN} self_send", True),
        (f"Re: {MARKER} {RUN} self_send", True),
        (f"RE: Fwd: {MARKER} {RUN} x", True),
        (f"Hello {MARKER} {RUN}", False),
        (f"{MARKER} notes", False),
        (f"{MARKER.lower()} {RUN}", False),
        (f"{MARKER}{RUN}", False),
        ("Lunch", False),
        ("", False),
    ],
)
def test_only_a_subject_that_opens_with_the_marker_carries_it(subject: str, expected: bool) -> None:
    assert carries_marker(subject) is expected


def test_run_ids_sort_by_time_and_name_their_marker() -> None:
    first = new_run_id(datetime(2026, 11, 24, 15, 30, tzinfo=UTC), "ab12ffff")
    later = new_run_id(datetime(2026, 11, 25, 9, 0, tzinfo=UTC), "0000")
    assert first == "20261124T153000-ab12"
    assert first < later
    assert run_of(subject_for(first, "self_send x")) == first
    assert run_marker(first) == f"{MARKER} {first}"


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


def test_the_score_counts_passes_over_runs_and_leftovers_as_failures() -> None:
    results = (
        result("a", Status.PASS),
        result("b", Status.PASS),
        result("c", Status.FAIL, "broke"),
        result("d", Status.SKIPPED, "no source"),
    )
    plain = compute_score(results, 0)
    assert (plain.passed, plain.failed, plain.skipped) == (2, 1, 1)
    assert plain.percent == 67
    assert plain.coverage_percent == 75
    dirty = compute_score(results, 1)
    assert dirty.failed == 2
    assert dirty.percent == 50


def test_a_run_with_nothing_run_scores_one_hundred_with_no_coverage() -> None:
    score = compute_score((result("a", Status.SKIPPED, "off"),), 0)
    assert score.percent == 100
    assert score.coverage_percent == 0


def test_reports_are_written_as_json_and_markdown_and_read_back(tmp_path: Path) -> None:
    done = report((result("mail.self_send[mail:a]", Status.PASS),))
    json_path, md_path = write_report(tmp_path, done, datetime(2026, 11, 24, 16, 0, tzinfo=UTC))
    assert json_path == report_dir(tmp_path) / f"{RUN}.json"
    assert md_path.read_text(encoding="utf-8").startswith(f"# Live test {RUN}")
    assert read_reports(tmp_path) == [done]
    assert not list(report_dir(tmp_path).glob("*.tmp"))


def test_a_report_holds_no_subject_address_or_body(tmp_path: Path) -> None:
    done = report(
        (ScenarioResult(id="x", group="mail", status=Status.FAIL, reason="the reply failed"),),
        leftovers=("msg_aaaaaaaaaaaa",),
    )
    text = done.model_dump_json() + markdown(done) + table(done)
    assert MARKER not in text
    assert "@" not in text


def test_old_and_excess_reports_are_trimmed(tmp_path: Path) -> None:
    now = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    folder = report_dir(tmp_path)
    folder.mkdir(parents=True)
    old = folder / "20260101T000000-0000.json"
    old.write_text("{}", encoding="utf-8")
    stamp = (now - timedelta(days=30)).timestamp()
    os.utime(old, (stamp, stamp))
    fresh = folder / "20261124T150000-0000.json"
    fresh.write_text("{}", encoding="utf-8")
    assert trim_reports(tmp_path, now) == 1
    assert not old.exists()
    assert fresh.exists()


def test_an_unreadable_report_is_skipped(tmp_path: Path) -> None:
    folder = report_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "20261124T150000-0000.json").write_text("not json", encoding="utf-8")
    (folder / "notes.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    assert read_reports(tmp_path) == []


def test_the_table_ends_with_the_score_line() -> None:
    done = report(
        (result("a", Status.PASS), result("b", Status.FAIL, "broke")), ("msg_aaaaaaaaaaaa",)
    )
    lines = table(done).splitlines()
    assert any(line.startswith("  FAIL  b") and "broke" in line for line in lines)
    assert "Score 33%: 1 passed, 2 failed" in table(done)
    assert "left behind 1" in table(done)
