# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""Sessions: docs/contracts/07-attention.md (A-10) and board ruling R10."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chamberlain.app.core import App
from chamberlain.attention import repo
from chamberlain.limits import SESSION_IDLE_HOURS
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ErrorCode


def session_of(reply: Any) -> str:
    return str(reply.data.session)


def test_a10_a_new_start_supersedes_the_same_agent_and_purpose(app: App, ctx: Any):
    first = session_of(app.call("start", {"agent": "claude"}))
    second = session_of(app.call("start", {"agent": "claude"}))
    assert first != second
    old = repo.get_session(ctx.db, first)
    assert old is not None
    assert old.ended_at is not None
    live = repo.get_session(ctx.db, second)
    assert live is not None
    assert live.ended_at is None


def test_r10_a_scheduled_run_and_a_live_chat_never_supersede_each_other(app: App, ctx: Any):
    chat = session_of(app.call("start", {"agent": "claude", "purpose": "interactive"}))
    run = session_of(app.call("start", {"agent": "claude", "purpose": "scheduled"}))
    for handle in (chat, run):
        row = repo.get_session(ctx.db, handle)
        assert row is not None
        assert row.ended_at is None


def test_r10_other_agents_are_left_alone(app: App, ctx: Any):
    one = session_of(app.call("start", {"agent": "claude"}))
    session_of(app.call("start", {"agent": "codex"}))
    row = repo.get_session(ctx.db, one)
    assert row is not None
    assert row.ended_at is None


def test_a10_resume_returns_the_same_live_session(app: App):
    first = session_of(app.call("start", {"agent": "x"}))
    again = session_of(app.call("start", {"agent": "x", "resume": first}))
    assert again == first


def test_a10_resuming_an_unknown_or_ended_session_starts_a_new_one(app: App):
    first = session_of(app.call("start", {"agent": "x"}))
    app.call("start", {"agent": "x"})
    revived = session_of(app.call("start", {"agent": "x", "resume": first}))
    assert revived != first
    unknown = session_of(app.call("start", {"resume": "ses_ffffffffffffffff"}))
    assert unknown != "ses_ffffffffffffffff"


def test_a10_an_idle_session_ends_and_its_ledger_is_deleted(app: App, ctx: Any, clock: ManualClock):
    session = session_of(app.call("start", {}))
    assert ctx.db.execute("SELECT COUNT(*) FROM guidance_deliveries").fetchone()[0] > 0
    clock.advance(hours=SESSION_IDLE_HOURS + 1)
    app.call("doctor", {})  # any tool call ticks
    app.call("guide", {})
    row = repo.get_session(ctx.db, session)
    assert row is not None
    assert row.ended_at is not None
    assert ctx.db.execute("SELECT COUNT(*) FROM guidance_deliveries").fetchone()[0] == 0


def test_a10_activity_keeps_a_session_alive(app: App, ctx: Any, clock: ManualClock):
    session = session_of(app.call("start", {}))
    for _ in range(3):
        clock.advance(hours=SESSION_IDLE_HOURS - 1)
        assert app.call("next", {"session": session}).ok
    row = repo.get_session(ctx.db, session)
    assert row is not None
    assert row.ended_at is None


def test_a10_old_ended_sessions_are_cleaned_up(app: App, ctx: Any, clock: ManualClock):
    session = session_of(app.call("start", {}))
    clock.advance(hours=SESSION_IDLE_HOURS + 1)
    app.call("guide", {})
    clock.advance(hours=30)
    app.call("guide", {})
    assert repo.get_session(ctx.db, session) is None


def test_r10_a_scheduled_run_from_an_earlier_day_must_stop(app: App, ctx: Any, clock: ManualClock):
    clock.set(datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    run = session_of(app.call("start", {"agent": "claude", "purpose": "scheduled"}))
    clock.advance(hours=48)
    resumed = app.call("start", {"agent": "claude", "purpose": "scheduled", "resume": run})
    assert not resumed.ok
    assert resumed.error is not None
    assert resumed.error.code is ErrorCode.SESSION_STALE
    assert "Stop" in resumed.error.message


def test_r10_a_stale_scheduled_session_cannot_pull_more_work(
    app: App, clock: ManualClock, seed: Any
):
    clock.set(datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    run = session_of(app.call("start", {"purpose": "scheduled"}))
    clock.advance(hours=47)
    stopped = app.call("next", {"session": run})
    assert not stopped.ok


def test_r10_a_live_chat_on_a_later_day_is_not_stopped(app: App, clock: ManualClock):
    clock.set(datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    chat = session_of(app.call("start", {"purpose": "interactive"}))
    clock.advance(hours=47)
    resumed = app.call("start", {"resume": chat})
    assert resumed.ok


def test_r10_a_stale_scheduled_session_is_stopped_for_every_tool(app: App, clock: ManualClock):
    clock.set(datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    run = session_of(app.call("start", {"purpose": "scheduled"}))
    clock.advance(hours=47)
    for tool, request in (
        ("next", {}),
        ("find", {"scope": "mail"}),
        ("do", {"operation": "op_0123456789abcdef"}),
    ):
        reply = app.call(tool, {"session": run, **request})
        assert reply.error is not None
        assert reply.error.code is ErrorCode.SESSION_STALE


def test_r10_a_scheduled_start_for_another_day_is_stopped_without_resume(
    app: App, clock: ManualClock
):
    clock.set(datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    wrong = app.call("start", {"purpose": "scheduled", "scheduled_day": "2025-12-31"})
    assert wrong.error is not None
    assert wrong.error.code is ErrorCode.SESSION_STALE
    assert "session.stopped" in [g.id for g in wrong.guidance]
    today = clock.now().astimezone().date().isoformat()
    assert app.call("start", {"purpose": "scheduled", "scheduled_day": today}).ok


def test_r10_the_wrong_day_guard_uses_the_configured_zone_not_the_os_zone(
    app: App, clock: ManualClock, data_root: Path
):
    (data_root / "config").mkdir(parents=True)
    (data_root / "config" / "connections.yaml").write_text(
        "version: 1\ntimezone: Pacific/Auckland\n", encoding="utf-8"
    )
    clock.set(datetime(2026, 1, 5, 20, 0, tzinfo=UTC))  # already 6 January in Auckland
    behind = app.call("start", {"purpose": "scheduled", "scheduled_day": "2026-01-05"})
    assert behind.error is not None
    assert behind.error.code is ErrorCode.SESSION_STALE
    assert app.call("start", {"purpose": "scheduled", "scheduled_day": "2026-01-06"}).ok


def test_r10_without_a_scheduled_day_a_start_is_a_live_chat(app: App, clock: ManualClock):
    assert app.call("start", {"purpose": "scheduled"}).ok
    assert app.call("start", {"purpose": "interactive", "scheduled_day": "2020-01-01"}).ok
