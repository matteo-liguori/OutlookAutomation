"""Viewer notes (`user.note`) and the tag checker (`update.available`): 07-attention.md."""

import subprocess
from typing import Any

import pytest
from runs_rig import Rig

from chamberlain.app.wiring import COMPACTORS, run_tick
from chamberlain.memory.sweeper import sweep
from chamberlain.runs import repo
from chamberlain.runs.notes import add_note, clean_note, withdraw_note
from chamberlain.runs.updates import (
    check_for_update,
    git_tag_lister,
    newest_after,
    parse_installed,
    parse_tag,
    recorded_update,
)
from chamberlain.schema.errors import ChamberlainError
from chamberlain.writes import repo as write_repo


def live(rig: Rig, kind: str) -> list[Any]:
    return rig.rows("SELECT * FROM events WHERE kind = ? AND state IN ('new', 'delivered')", kind)


def test_a_viewer_note_becomes_a_user_note_event_until_it_is_withdrawn(rig: Rig) -> None:
    number = add_note(rig.ctx, "  Please   chase the\nplumber on Friday ")
    run_tick(rig.app)
    events = live(rig, "user.note")
    assert [e["summary"] for e in events] == [
        "Note from the user: Please chase the plumber on Friday"
    ]
    started = rig.start()
    assert [w["kind"] for w in started["data"]["work"]] == ["user.note"]
    withdraw_note(rig.ctx, number)
    run_tick(rig.app)
    assert live(rig, "user.note") == []


def test_a_note_audits_a_hash_never_its_text(rig: Rig) -> None:
    add_note(rig.ctx, "private words here")
    rows = [r for r in write_repo.audit_rows(rig.ctx.db) if r.kind == "user.note"]
    assert len(rows) == 1
    assert rows[0].actor == "human:viewer"
    assert "private words" not in rows[0].summary
    assert rows[0].payload_sha256 is not None


def test_a_note_must_be_one_short_printable_line_and_1_related_checks(rig: Rig) -> None:
    # a_note_must_be_one_short_printable_line
    for text in ["", "   ", "x" * 151, "bell\x07"]:
        with pytest.raises(ChamberlainError):
            clean_note(text)

    # a_note_stops_raising_after_seven_days_and_is_deleted_after_fourteen
    add_note(rig.ctx, "old note")
    run_tick(rig.app)
    assert len(live(rig, "user.note")) == 1
    rig.clock.advance(hours=8 * 24)
    run_tick(rig.app)
    assert live(rig, "user.note") == []
    assert len(repo.notes_since(rig.ctx.db, "2000-01-01T00:00:00Z")) == 1
    rig.clock.advance(hours=7 * 24)
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    assert repo.notes_since(rig.ctx.db, "2000-01-01T00:00:00Z") == []


def test_tags_are_ordered_by_release_and_only_well_and_1_related_checks(
    rig: Rig,
) -> None:
    # tags_are_ordered_by_release_and_only_well_formed_ones_count
    assert parse_tag("chamberlain/v2.0.0-alpha.1") < parse_tag("chamberlain/v2.0.0")  # type: ignore[operator]
    assert parse_tag("chamberlain/v2.0.0-rc.1") < parse_tag("chamberlain/v2.0.0")  # type: ignore[operator]
    assert parse_tag("other/v2.0.0") is None
    assert parse_tag("chamberlain/v2.0") is None
    assert parse_installed("2.0.0a0") == (2, 0, 0, 0, 0)
    tags = ["chamberlain/v1.0.0", "chamberlain/v2.0.0-alpha.1", "chamberlain/v2.1.0", "junk"]
    assert newest_after(tags, "2.0.0a0") == "chamberlain/v2.1.0"
    assert newest_after(tags, "2.1.0") is None
    assert newest_after([], "2.0.0a0") is None

    # the_checker_asks_once_a_day_records_a_tag_and_raises_the_event
    asked: list[int] = []

    def lister() -> list[str]:
        asked.append(1)
        return ["chamberlain/v9.0.0"]

    assert check_for_update(rig.ctx, lister) == "chamberlain/v9.0.0"
    assert check_for_update(rig.ctx, lister) == "chamberlain/v9.0.0"
    assert len(asked) == 1
    run_tick(rig.app)
    assert [e["summary"] for e in live(rig, "update.available")] == [
        "A newer release, chamberlain/v9.0.0, is available. Offer to tell the user."
    ]
    rig.clock.advance(hours=25)
    assert check_for_update(rig.ctx, lambda: []) is None
    run_tick(rig.app)
    assert live(rig, "update.available") == []
    assert recorded_update(rig.ctx) is None


def test_a_failing_lister_keeps_the_earlier_answer_and_and_1_related_checks(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a_failing_lister_keeps_the_earlier_answer_and_retries_in_an_hour
    check_for_update(rig.ctx, lambda: ["chamberlain/v9.0.0"])
    rig.clock.advance(hours=25)

    def broken() -> list[str]:
        raise OSError("offline")

    assert check_for_update(rig.ctx, broken) == "chamberlain/v9.0.0"
    calls: list[int] = []
    rig.clock.advance(minutes=30)
    check_for_update(rig.ctx, lambda: calls.append(1) or [])
    assert calls == []
    rig.clock.advance(minutes=31)
    assert check_for_update(rig.ctx, lambda: calls.append(1) or []) is None
    assert calls == [1]

    # the_git_lister_reads_remote_tags_only
    seen: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        lines = ["aa", "bb", "cc"]
        names = ["chamberlain/v1.0.0", "chamberlain/v1.0.0^{}", "x"]
        out = chr(10).join(
            f"{sha}{chr(9)}refs/tags/{name}" for sha, name in zip(lines, names, strict=True)
        )
        return subprocess.CompletedProcess(command, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert git_tag_lister("origin")() == ["chamberlain/v1.0.0", "x"]
    assert seen == [["git", "ls-remote", "--tags", "origin"]]


def test_no_tick_checks_for_updates_unless_a_lister_is_given(rig: Rig) -> None:
    run_tick(rig.app)
    rig.clock.advance(hours=48)
    run_tick(rig.app)
    assert recorded_update(rig.ctx) is None
    assert rig.rows("SELECT * FROM system_state WHERE key = 'runs.update_checked_at'") == []
