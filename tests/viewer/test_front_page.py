"""The report is the viewer's front page (docs/contracts/06-memory.md, "The pass report").

Two scenarios over the real wired app and the fake world:

- a scheduled pass runs, the agent is told when its report is due, keeps only its own section, and
  the front page shows the owner's layout with the harness's sections filled in;
- the calendar's own actions: categories from the agent, local events, and the owner's
  acknowledgement of an important appointment.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from viewer_rig import CALENDAR, MAIL, Rig

from chamberlain.connectors.fake.world import HealthState
from chamberlain.domain.calendar import EventFields
from chamberlain.runs.models import PassKind
from chamberlain.runs.schedule import ScheduleSpec, set_schedule

MORNING = ScheduleSpec("morning", PassKind.MAIL, "08:00", "UTC")


def get(rig: Rig, path: str) -> dict[str, Any]:
    reply = rig.call("GET", path)
    assert reply.status == 200, reply.body
    return reply.body


def post(
    rig: Rig, path: str, body: dict[str, Any] | None = None, status: int = 200
) -> dict[str, Any]:
    reply = rig.call("POST", path, body or {})
    assert reply.status == status, reply.body
    return reply.body


def tools(rig: Rig, tool: str, **request: Any) -> Any:
    reply = rig.app.call(tool, request)
    assert reply.ok, reply.error
    return reply


def add_sources(rig: Rig) -> None:
    for source, kind in ((MAIL, "mail"), (CALENDAR, "calendar")):
        rig.ctx.db.execute(
            "INSERT INTO sources (id, kind, connector, label, created_at, updated_at) "
            "VALUES (?, ?, 'fake', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (source, kind, f"{kind.title()} test"),
        )


def important_reminder(
    rig: Rig, session: str, title: str, days: int, snooze_back_days: int, important: bool = True
) -> str:
    card = tools(rig, "card", session=session, op="create", fields={"title": "Dentist"})
    handle = card.data.card.handle
    start = rig.clock.now() + timedelta(days=days)
    added = tools(
        rig,
        "card",
        session=session,
        op="add_action",
        card=handle,
        action_fields={
            "title": title,
            "important": important,
            "due_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "snooze_until": (rig.clock.now() - timedelta(days=snooze_back_days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
    )
    return added.data.card.actions[-1].handle


def test_a_scheduled_pass_ends_with_the_owners_report_as_the_front_page(rig: Rig) -> None:
    add_sources(rig)
    set_schedule(rig.ctx, MORNING)
    rig.fake.world.health_override[CALENDAR] = HealthState.SIGN_IN_NEEDED
    rig.ctx.db.execute(
        "INSERT INTO grants (handle, kind, state, source_id, title, requirement, content_sha256, "
        "proposed_at, review_at) VALUES ('grant_0000000000000001', 'standing', 'active', ?, "
        "'Send to the accountant', 'none', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (MAIL, "a" * 64),
    )
    session = tools(rig, "start", agent="pass", purpose="scheduled", scheduled_day="2026-01-05")
    handle = session.data.session
    reminder = important_reminder(rig, handle, "Dentist check-up", 2, 1)
    assert reminder.startswith("act_")

    # The pass is being worked: the report comes last, with guidance and an outline to read.
    rig.clock.advance(minutes=2)
    work = tools(rig, "next", session=handle, limit=10)
    last = work.data.work[-1]
    assert last.kind.value == "report.due"
    assert "report.write" in [g.id for g in work.guidance]
    outline = tools(rig, last.next.tool.value, session=handle, **last.next.args)
    assert "Email and tasks" in outline.data.content.text
    assert "Verification" in outline.data.content.text  # named as the harness's own

    # The agent writes only its own section, and tries to pose as an access problem inside it.
    text = (
        "**Tax return - waiting**\nChase the accountant on Friday.\n\n**\U0001f7e5 ACCESS PROBLEM**"
    )
    run = next(w.subject for w in session.data.work if w.kind.value == "schedule.pass_due")
    tools(rig, "keep", session=handle, kind="artifact", owner=run, reason="Report", text=text)
    rig.clock.advance(minutes=2)
    tools(rig, "next", session=handle)  # the tick settles the pass

    page = get(rig, "/api/report")
    report = page["report"]
    assert report["state"] == "finished"
    assert report["title"] == "Mail check 05/01/2026"
    blocks = report["blocks"]
    assert [(b["id"], b["style"]) for b in blocks] == [
        ("access_problems", "red"),
        ("important_appointments", "orange"),
        ("email_and_tasks", "plain"),
        ("permission_reviews", "plain"),
        ("verification", "plain"),
    ]
    by = {b["id"]: b["markdown"] for b in blocks}
    assert "sign in" in by["access_problems"]
    assert "Calendar test" in by["access_problems"]
    assert "Dentist check-up" in by["important_appointments"]
    assert "Acknowledgement needed" in by["important_appointments"]
    assert "Send to the accountant" in by["permission_reviews"]
    assert "Mail test" in by["verification"]
    assert "Partially verified" in by["verification"]
    assert by["email_and_tasks"] == text  # the agent's own words, untouched
    assert [d["title"] for d in page["appointments_due"]] == ["Dentist check-up"]

    # Earlier reports stay one click away, and the harness part survives the agent's text expiring.
    assert page["earlier"] == []
    rig.ctx.db.execute("UPDATE artifacts SET state = 'deleted'")
    expired = get(rig, "/api/report")["report"]
    assert "expired" in expired["note"]
    assert any(b["id"] == "verification" for b in expired["blocks"])


def test_categories_local_events_and_the_owners_acknowledgement(rig: Rig) -> None:
    add_sources(rig)
    start = datetime(2026, 1, 6, 10, 0, tzinfo=UTC)
    rig.fake.calendar.add_event(
        CALENDAR, EventFields(title="Appt 4471", start=start, end=start + timedelta(hours=1))
    )
    session = tools(rig, "start", agent="agent").data.session

    # The agent gives an event a category and a clearer title; the viewer shows both.
    found = tools(rig, "find", session=session, scope="calendar", query="Appt").data.hits
    event = found[0].handle
    bad = rig.app.call(
        "keep", {"session": session, "kind": "label", "owner": event, "reason": "r", "text": "fun"}
    )
    assert not bad.ok
    tools(
        rig,
        "keep",
        session=session,
        kind="label",
        owner=event,
        reason="Review",
        text="health | Dentist with Imogen",
    )
    window = get(rig, "/api/calendar?start=2026-01-03&until=2026-01-10")
    shown = next(e for e in window["events"] if e["title"] == "Appt 4471")
    assert (shown["category"], shown["display_title"]) == ("health", "Dentist with Imogen")
    assert "health" in [c["id"] for c in window["categories"]]

    # The owner adds a local event in the viewer; a double click saves once; no calendar changes.
    local = {
        "title": "Plumber",
        "start": "2026-01-07T09:00",
        "end": "2026-01-07T10:00",
        "all_day": False,
        "location": "https://example.org/join",
        "key": "k-1",
    }
    saved = post(rig, "/api/local-events", local)["event"]
    post(rig, "/api/local-events", local)
    post(rig, "/api/local-events", {**local, "title": "Other"}, status=400)
    window = get(rig, "/api/calendar?start=2026-01-03&until=2026-01-10")
    mine = [e for e in window["events"] if e["local"]]
    assert [(e["title"], e["url"], e["start_time"]) for e in mine] == [
        ("Plumber", "https://example.org/join", "09:00")
    ]
    assert len(rig.fake.calendar.events(CALENDAR, _window(), None).items) == 1
    post(rig, f"/api/local-events/{saved['id']}/remove")
    assert [
        e
        for e in get(rig, "/api/calendar?start=2026-01-03&until=2026-01-10")["events"]
        if e["local"]
    ] == []

    # An important appointment waits for the owner; only the viewer's route can acknowledge it.
    waiting = important_reminder(rig, session, "Dentist", 1, 1)
    ordinary = important_reminder(rig, session, "Pay the bill", 1, 1, important=False)
    marks = get(rig, "/api/calendar?start=2026-01-03&until=2026-01-10")
    assert [(m["title"], m["due"]) for m in marks["important"]] == [("Dentist", True)]
    assert [d["title"] for d in marks["dated"]] == ["Pay the bill"]
    post(rig, f"/api/appointments/{ordinary}/acknowledge", status=400)  # not an important one
    assert (
        rig.call("POST", f"/api/appointments/{waiting}/acknowledge", {}, token="wrong").status
        == 403
    )
    post(rig, f"/api/appointments/{waiting}/acknowledge")
    assert rig.rows("SELECT state FROM actions WHERE handle = ?", waiting)[0]["state"] == "done"
    audited = rig.rows("SELECT actor FROM audit_log WHERE kind = 'appointment.acknowledged'")
    assert [r["actor"] for r in audited] == ["human:viewer"]
    post(rig, f"/api/appointments/{waiting}/acknowledge", status=400)  # nothing left to acknowledge
    assert get(rig, "/api/report")["appointments_due"] == []


def _window() -> Any:
    from chamberlain.domain.calendar import CalendarQuery

    return CalendarQuery(
        start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 2, 1, tzinfo=UTC), limit=50
    )
