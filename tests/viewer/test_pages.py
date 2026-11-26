"""The redesigned pages: calendar, reports below each day, card grid, timeline, uncertain page.

Each page's data is a read-only view built from the store (and, for the calendar, the read-only
`events` call of each calendar source). No sockets, no browser.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from viewer_rig import CALENDAR, Rig

from chamberlain.app.wiring import run_tick
from chamberlain.domain.calendar import EventFields
from chamberlain.runs import repo as runs_repo
from chamberlain.runs.models import PassKind
from chamberlain.runs.reports import store_report
from chamberlain.runs.schedule import ScheduleSpec, set_schedule
from chamberlain.schema.errors import ChamberlainError, ErrorCode

OLD = "2025-12-30T08:00:00Z"
NOW = "2026-01-05T08:00:00Z"


def get(rig: Rig, path: str) -> dict[str, Any]:
    reply = rig.call("GET", path)
    assert reply.status == 200, reply.body
    return reply.body


def add_event(rig: Rig, title: str, start: datetime, hours: int = 1, **kwargs: Any) -> None:
    rig.fake.calendar.add_event(
        CALENDAR,
        EventFields(title=title, start=start, end=start + timedelta(hours=hours), **kwargs),
    )


def make_card(rig: Rig, title: str, goal: str = "") -> str:
    started = rig.app.call("start", {"agent": "agent"})
    session = started.data.session  # type: ignore[union-attr]
    fields = {"title": title, **({"goal": goal} if goal else {})}
    made = rig.app.call("card", {"session": session, "op": "create", "fields": fields})
    assert made.ok, made.error
    return made.data.card.handle  # type: ignore[union-attr]


def add_action(
    rig: Rig, handle: str, card: str, title: str, due: str | None, state: str = "open"
) -> None:
    rig.ctx.db.execute(
        "INSERT INTO actions (handle, card, title, state, due_at, created_at, updated_at, "
        "closed_at, outcome) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z', ?, ?)",
        (
            handle,
            card,
            title,
            state,
            due,
            None if state == "open" else "2026-01-04T10:00:00Z",
            None if state == "open" else "Paid.",
        ),
    )


# --- calendar ---------------------------------------------------------------------------------


def test_the_calendar_page_lists_days_and_sources_asks_a_sane_window_and_needs_the_token(
    rig: Rig,
) -> None:
    # the_calendar_lists_events_by_day_with_a_tone_per_calendar
    add_event(rig, "Standup", datetime(2026, 1, 5, 9, 30, tzinfo=UTC), 1, location="Room 2")
    add_event(rig, "Offsite", datetime(2026, 1, 6, 0, 0, tzinfo=UTC), 48, all_day=True)
    add_event(rig, "Out of window", datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
    data = get(rig, "/api/calendar?start=2026-01-03&until=2026-01-10")
    assert data["today"] == "2026-01-05"
    assert [s["id"] for s in data["sources"]] == [CALENDAR]
    assert data["sources"][0]["read"] is True
    titles = {e["title"]: e for e in data["events"]}
    assert set(titles) == {"Standup", "Offsite"}
    standup = titles["Standup"]
    assert (standup["first_day"], standup["start_time"], standup["end_time"]) == (
        "2026-01-05",
        "09:30",
        "10:30",
    )
    assert standup["location"] == "Room 2"
    assert standup["tone"] == data["sources"][0]["tone"]
    assert standup["calendars"] == [CALENDAR]
    offsite = titles["Offsite"]
    assert offsite["all_day"] is True
    assert (offsite["first_day"], offsite["last_day"]) == ("2026-01-06", "2026-01-07")

    # the_calendar_says_when_a_source_could_not_be_read
    def broken(*_: Any, **__: Any) -> Any:
        raise ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, "down")

    rig.ctx.sources.calendar[CALENDAR].events = broken  # type: ignore[method-assign]
    data = get(rig, "/api/calendar?start=2026-01-03&until=2026-01-10")
    assert data["events"] == []
    assert data["sources"][0]["read"] is False
    assert "could not be read" in data["sources"][0]["note"]

    # the_calendar_asks_for_a_sane_window
    assert rig.call("GET", "/api/calendar?start=2026-01-03&until=2026-06-10").status == 400
    assert rig.call("GET", "/api/calendar?start=2026-01-10&until=2026-01-03").status == 400
    assert rig.call("GET", "/api/calendar?start=nope").status == 400

    # dated_actions_show_on_the_calendar_but_are_not_important_appointments
    card = make_card(rig, "Tax return")
    add_action(rig, "act_0123456789abcdef", card, "File it", "2026-01-06T12:00:00Z")
    data = get(rig, "/api/calendar?start=2026-01-03&until=2026-01-10")
    assert data["important"] == []
    assert [(m["day"], m["title"], m["card_title"], m["due"]) for m in data["dated"]] == [
        ("2026-01-06", "File it", "Tax return", False)
    ]

    # the_calendar_needs_the_token
    for path in ("/api/calendar", "/api/reports?day=2026-01-05", "/api/cards", "/api/timeline"):
        assert rig.call("GET", path, token="wrong").status == 403
        assert rig.call("GET", path, host="evil.example").status == 403
        assert rig.call("GET", path, remote="10.0.0.5").status == 403


# --- reports below each day -------------------------------------------------------------------


def test_reports_are_listed_by_day_and_read_below_the_calendar(rig: Rig) -> None:
    set_schedule(rig.ctx, ScheduleSpec("morning", PassKind.MAIL, "08:00", "UTC"))
    rig.ctx.db.execute("UPDATE schedules SET created_at = '2026-01-01T00:00:00Z'")
    # an older schedule: its 08:00 pass today is owed (a new one would not be)
    run_tick(rig.app)
    run = runs_repo.recent_runs(rig.ctx.db, 1)[0]
    store_report(rig.ctx, run.handle, "# Morning\n- Three messages filed.")
    data = get(rig, "/api/calendar?start=2026-01-03&until=2026-01-10")
    assert data["report_days"] == [run.day]
    shown = get(rig, f"/api/reports?day={run.day}")
    assert shown["runs"][0]["schedule"] == "morning"
    blocks = shown["runs"][0]["report"]["blocks"]
    assert [b["markdown"] for b in blocks if b["by"] == "agent"] == [
        "# Morning\n- Three messages filed."
    ]
    assert get(rig, "/api/reports?day=2020-01-01")["runs"] == []
    assert rig.call("GET", "/api/reports?day=bad").status == 400


# --- card grid --------------------------------------------------------------------------------


# --- timeline ---------------------------------------------------------------------------------


# --- the uncertain page -----------------------------------------------------------------------


def insert_imported(
    rig: Rig, kind: str, source: str, summary: str | None, attempted: str | None
) -> None:
    rig.ctx.db.execute(
        "INSERT INTO operations (handle, idempotency_key, kind, source_id, state, intent_sha256, "
        "summary_json, attempted_at, created_at, updated_at, dispatched_at) VALUES "
        "('op_0123456789abcdef', 'import:x', ?, ?, 'uncertain', ?, ?, ?, ?, ?, ?)",
        (kind, source, "a" * 64, summary, attempted, NOW, NOW, NOW),
    )
