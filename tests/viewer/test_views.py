"""What the viewer shows: needs-you counts, uncertain operations, today's audit, cards, runs,
reports, storage and notes. Read-only views of the other tracks' tables (06-memory.md,
10-targets.md) and the viewer's two writes, both through the runs track.
"""

from typing import Any

from viewer_rig import Rig

from chamberlain.app.wiring import run_tick
from chamberlain.runs import repo as runs_repo
from chamberlain.runs.models import PassKind
from chamberlain.runs.reports import store_report
from chamberlain.runs.schedule import ScheduleSpec, set_schedule


def get(rig: Rig, path: str) -> dict[str, Any]:
    reply = rig.call("GET", path)
    assert reply.status == 200, reply.body
    return reply.body


def test_the_overview_counts_what_needs_the_person(rig: Rig) -> None:
    assert get(rig, "/api/overview")["needs_you"]["grants_to_confirm"] == 0
    rig.proposal(to="x@example.org")
    rig.proposal(to="y@example.org")
    data = get(rig, "/api/overview")
    assert data["needs_you"]["grants_to_confirm"] == 2
    assert data["counts"]["active_grants"] == 0
    assert data["over_budget"] == "0"


def test_an_uncertain_operation_shows_its_exact_text_and_can_be_resolved(rig: Rig) -> None:
    proposal = rig.proposal(subject="Did this go out?")
    operation = proposal.operation.handle  # type: ignore[union-attr]
    rig.ctx.db.execute(
        "UPDATE operations SET state = 'uncertain', dispatched_at = ? WHERE handle = ?",
        ("2026-01-05T08:59:00Z", operation),
    )
    shown = get(rig, "/api/uncertain")["operations"]
    assert [o["handle"] for o in shown] == [operation]
    assert "Did this go out?" in shown[0]["rendered"]
    assert "The exact body text." in shown[0]["rendered"]
    assert get(rig, "/api/overview")["needs_you"]["uncertain_operations"] == 1
    done = rig.call("POST", f"/api/operations/{operation}/resolve", {"outcome": "did_not_happen"})
    assert done.status == 200, done.body
    assert done.body["state"] == "confirmed_absent"
    assert get(rig, "/api/uncertain")["operations"] == []


def test_today_lists_what_the_assistant_did_from_the_audit_log(rig: Rig) -> None:
    started = rig.app.call("start", {"agent": "agent"})
    session = started.data.session  # type: ignore[union-attr]
    rig.app.call("next", {"session": session})
    rig.proposal()
    data = get(rig, "/api/activity")
    assert data["day"] == "2026-01-05"
    assert data["tool_calls"]["start"] == 1
    assert data["tool_calls"]["next"] == 1
    assert any(
        e["kind"].startswith("grant.") or e["kind"].startswith("operation.") for e in data["events"]
    )
    assert all(e["kind"] != "tool.call" for e in data["events"])
    assert get(rig, "/api/activity?day=2020-01-01")["events"] == []
    assert rig.call("GET", "/api/activity?day=not-a-day").status == 400


def test_cards_and_due_actions_are_listed(rig: Rig) -> None:
    started = rig.app.call("start", {"agent": "agent"})
    session = started.data.session  # type: ignore[union-attr]
    made = rig.app.call(
        "card", {"session": session, "op": "create", "fields": {"title": "Fix the boiler"}}
    )
    assert made.ok, made.error
    data = get(rig, "/api/work")
    assert [c["title"] for c in data["cards"]] == ["Fix the boiler"]
    assert data["actions"] == []


def test_a_run_report_is_shown_and_then_gone_when_it_expires(rig: Rig) -> None:
    set_schedule(rig.ctx, ScheduleSpec("morning", PassKind.MAIL, "08:00", "UTC"))
    run_tick(rig.app)
    run = runs_repo.recent_runs(rig.ctx.db, 1)[0]
    store_report(rig.ctx, run.handle, "Three messages filed. Nothing needs you.")
    listing = get(rig, "/api/runs")
    assert listing["schedules"][0]["name"] == "morning"
    assert listing["runs"][0]["reports"] == 1
    shown = get(rig, f"/api/runs/{run.handle}/reports")
    assert [r["text"] for r in shown["reports"]] == ["Three messages filed. Nothing needs you."]
    rig.clock.advance(hours=15 * 24)
    rig.ctx.db.execute("UPDATE artifacts SET state = 'released'")
    assert get(rig, f"/api/runs/{run.handle}/reports")["reports"] == []
    assert rig.call("GET", "/api/runs/run_0123456789abcdef/reports").status == 404


def test_storage_shows_the_figures_of_the_targets_contract(rig: Rig) -> None:
    data = get(rig, "/api/storage")
    for key in ("artifacts_live", "artifact_bytes", "db_bytes", "data_root_bytes"):
        assert isinstance(data[key], int)
    assert data["db_bytes"] > 0


def test_a_note_from_the_viewer_reaches_the_agent_and_can_be_withdrawn(rig: Rig) -> None:
    sent = rig.call("POST", "/api/notes", {"text": "Please look at the Smith invoice."})
    assert sent.status == 200
    number = sent.body["id"]
    assert get(rig, "/api/notes")["notes"][0]["text"] == "Please look at the Smith invoice."
    run_tick(rig.app)
    started = rig.app.call("start", {"agent": "agent"})
    work = started.data.work  # type: ignore[union-attr]
    assert [w.kind.value for w in work] == ["user.note"]
    assert "Smith invoice" in work[0].summary
    assert rig.call("POST", "/api/notes", {"text": "x" * 151}).status == 400
    assert rig.call("POST", "/api/notes", {}).status == 400
    assert rig.call("POST", f"/api/notes/{number}/withdraw", {}).status == 200
    assert get(rig, "/api/notes")["notes"][0]["state"] == "withdrawn"
    assert rig.call("POST", "/api/notes/9999/withdraw", {}).status == 404
