"""What waits for the owner, and the live report.

Rules: docs/contracts/06-memory.md (the pass report) and docs/scheduling.md (the live report).
A "Waiting for you" section lists every open action waiting for the owner. The newest pass's
report stays live until the next pass starts: its harness sections are recomposed on each read,
any session may replace its text, and the next pass's start freezes it.
"""

from typing import Any

import pytest
from runs_rig import Rig

from chamberlain.app.wiring import COMPACTORS, run_tick
from chamberlain.limits import ACTION_TITLE_MAX
from chamberlain.memory.sweeper import sweep
from chamberlain.runs import compose, repo
from chamberlain.runs.models import PassKind, RunRow, RunState
from chamberlain.runs.reports import read_reports
from chamberlain.runs.schedule import ScheduleSpec, set_schedule
from chamberlain.runs.template import load_template, private_path
from chamberlain.schema.tools import OWNER_ASKS_MAX

MORNING = ScheduleSpec("morning-mail", PassKind.MAIL, "08:00", "UTC")
ASK = "Diana (solicitor, 05 Jan 06:00) asks which Swiss university Giulia graduated from"


def ask(rig: Rig, title: str, card_title: str = "Giulia's diploma") -> str:
    card = rig.call("card", op="create", fields={"title": card_title})
    assert card["ok"], card
    added = rig.call(
        "card",
        op="add_action",
        card=card["data"]["card"]["handle"],
        action_fields={"title": title, "for_owner": True},
    )
    assert added["ok"], added
    return added["data"]["card"]["actions"][0]["handle"]


def chat(rig: Rig) -> dict[str, Any]:
    rig.session = None
    started = rig.start()
    assert started["ok"], started
    return started


def guidance(reply: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {g["id"]: g for g in reply.get("guidance", [])}


def run_pass(rig: Rig, text: str) -> RunRow:
    """The day's 08:00 pass: a scheduled session works it and keeps `text` as its report."""
    rig.clock.advance(hours=2)
    run_tick(rig.app)
    run = next(r for r in repo.recent_runs(rig.ctx.db, 5) if r.state is RunState.DUE)
    rig.session = None
    started = rig.start(purpose="scheduled", scheduled_day=run.day)
    assert started["ok"], started
    run_tick(rig.app)  # the pass starts
    kept = rig.call("keep", kind="artifact", owner=run.handle, reason="Report", text=text)
    assert kept["ok"], kept
    rig.clock.advance(minutes=2)
    run_tick(rig.app)  # the pass settles
    done = repo.get_run(rig.ctx.db, run.handle)
    assert done is not None
    assert done.state is RunState.FINISHED
    return done


def agent_text(report: dict[str, Any]) -> str:
    return next(b["markdown"] for b in report["blocks"] if b["id"] == "email_and_tasks")


def block(report: Any, slot: str) -> Any:
    blocks = report["blocks"] if isinstance(report, dict) else report.blocks
    return next((b for b in blocks if (b["id"] if isinstance(b, dict) else b.id) == slot), None)


def test_waiting_for_you_lists_every_ask_oldest_first_at_the_top(rig: Rig) -> None:
    set_schedule(rig.ctx, MORNING)
    chat(rig)
    rig.clock.advance(hours=2)
    run_tick(rig.app)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    assert block(compose.snapshot(rig.ctx, run), "waiting_for_you") is None  # none: left out

    ask(rig, ASK)
    rig.clock.advance(minutes=5)
    ask(rig, "Approve the reply to the landlord about the boiler", card_title="Flat [boiler]")
    rig.call(
        "card",
        op="add_action",
        card=rig.rows("SELECT handle FROM cards")[0]["handle"],
        action_fields={"title": "File the diploma scan"},
    )  # not for the owner
    report = compose.snapshot(rig.ctx, run)
    assert report.blocks[0].id == "waiting_for_you"
    assert report.blocks[0].style == "orange"
    lines = (report.blocks[0].markdown or "").splitlines()
    assert lines == [
        f"- **{ASK}** - Card: Giulia's diploma. Waiting since Mon 05 Jan 08:00.",
        "- **Approve the reply to the landlord about the boiler** - Card: Flat boiler. "
        "Waiting since Mon 05 Jan 08:05.",
    ]


def test_a_private_layout_without_the_section_gets_it_first(rig: Rig) -> None:
    path = private_path(rig.ctx.data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "version: 1\nslots:\n  - {id: notes, by: agent, heading: Notes}\n"
        "  - {id: verification, by: harness, heading: Verification}\n",
        encoding="utf-8",
    )
    template, problem = load_template(rig.ctx.data_root)
    assert problem is None
    assert [s.id for s in template.slots] == ["waiting_for_you", "notes", "verification"]
    path.write_text(
        "version: 1\nslots:\n  - {id: notes, by: agent, heading: Notes}\n"
        "  - {id: waiting_for_you, by: harness, heading: Asks}\n",
        encoding="utf-8",
    )
    template, _ = load_template(rig.ctx.data_root)
    assert [(s.id, s.heading) for s in template.slots] == [
        ("notes", "Notes"),
        ("waiting_for_you", "Asks"),
    ]


def test_owner_waiting_leads_a_start_only_when_something_waits(rig: Rig) -> None:
    quiet = chat(rig)
    assert "waiting_for_you" not in quiet["data"]
    assert "owner.waiting" not in guidance(quiet)

    ask(rig, ASK)
    busy = chat(rig)
    assert [a["title"] for a in busy["data"]["waiting_for_you"]] == [ASK]
    assert "word for word" in guidance(busy)["owner.waiting"]["text"]
    rig.session = None
    scheduled = rig.start(purpose="scheduled", scheduled_day="2026-01-05")
    assert "text" in guidance(scheduled)["owner.waiting"]  # any purpose

    # Many long asks: the start still fits, lists the oldest and says how many more there are.
    for number in range(OWNER_ASKS_MAX + 2):
        rig.clock.advance(minutes=1)
        ask(rig, f"{number:02d} " + "x" * (ACTION_TITLE_MAX - 3))
    full = chat(rig)
    assert full["ok"], full
    assert full["data"]["waiting_for_you"][0]["title"] == ASK
    assert len(full["data"]["waiting_for_you"]) == OWNER_ASKS_MAX
    assert full["data"]["waiting_more"] == 3
    assert "text" in guidance(full)["owner.waiting"]  # the full rule, not a pointer


def test_the_live_report_recomposes_takes_new_text_and_freezes_at_the_next_pass(rig: Rig) -> None:
    set_schedule(rig.ctx, MORNING)
    assert "live_report" not in chat(rig)["data"]  # no pass has reported yet
    first = run_pass(rig, "v1: nothing waits.")

    # report.live rides with a chat's start, never with a scheduled one.
    started = chat(rig)
    assert started["data"]["live_report"] == first.handle
    assert "keep your whole rewritten text" in guidance(started)["report.live"]["text"]
    rig.session = None
    scheduled = rig.start(purpose="scheduled", scheduled_day=first.day)
    assert "live_report" not in scheduled["data"]
    assert "report.live" not in guidance(scheduled)

    # Live: the harness sections are recomposed now, and the newest text wins.
    chat(rig)
    report = compose.read_composed(rig.ctx, first)
    assert report is not None
    assert report["live"] is True
    assert block(report, "waiting_for_you") is None
    ask(rig, ASK)
    rig.clock.advance(minutes=1)
    kept = rig.call("keep", kind="artifact", owner=first.handle, reason="Report", text="v2")
    assert kept["ok"], kept
    report = compose.read_composed(rig.ctx, first)
    assert report is not None
    assert ASK in block(report, "waiting_for_you")["markdown"]
    assert agent_text(report) == "v2"
    assert [r.text for r in read_reports(rig.ctx, first.handle)] == ["v2"]
    released = rig.rows(
        "SELECT state FROM artifacts WHERE filename IS NOT 'report-outline.txt' AND state <> 'live'"
    )
    assert [r["state"] for r in released] == ["released"]  # the old text goes, no orphan

    # The same text again replaces it too; the bytes stay readable after the sweeper's passes.
    rig.clock.advance(minutes=1)
    assert rig.call("keep", kind="artifact", owner=first.handle, reason="R", text="v2")["ok"]
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    assert [r.text for r in read_reports(rig.ctx, first.handle)] == ["v2"]

    # The next day's pass starts: the report freezes with its sections as of that moment.
    rig.clock.advance(hours=22)  # Tuesday 06:05
    second_due = rig.clock.now()
    rig.clock.advance(hours=2)
    run_tick(rig.app)
    second = next(r for r in repo.recent_runs(rig.ctx.db, 5) if r.state is RunState.DUE)
    rig.session = None
    rig.start(purpose="scheduled", scheduled_day=second.day)
    run_tick(rig.app)
    assert repo.get_run(rig.ctx.db, second.handle).state is RunState.RUNNING  # type: ignore[union-attr]
    assert second_due < rig.clock.now()
    frozen_at = rig.rows("SELECT frozen_at FROM run_reports WHERE run = ?", first.handle)
    assert frozen_at[0]["frozen_at"] is not None
    stored = compose.stored_snapshot(rig.ctx, first.handle)
    assert stored is not None
    assert ASK in (block(stored, "waiting_for_you").markdown or "")
    assert compose.freeze(rig.ctx, first) is False  # only once
    after = compose.read_composed(rig.ctx, first)
    assert after is not None
    assert after["live"] is False

    # Frozen: its text can no longer change. The error says where the live report is.
    refused = rig.call("keep", kind="artifact", owner=first.handle, reason="R", text="v3")
    assert refused["ok"] is False
    assert refused["error"]["code"] == "artifact.owner_ended"
    assert "No report is live" in refused["error"]["fix"]
    assert rig.call("keep", kind="artifact", owner=second.handle, reason="R", text="w1")["ok"]
    rig.clock.advance(minutes=2)
    run_tick(rig.app)
    refused = rig.call("keep", kind="artifact", owner=first.handle, reason="R", text="v3")
    assert second.handle in refused["error"]["fix"]
    assert [r.text for r in read_reports(rig.ctx, first.handle)] == ["v2"]


def test_an_older_report_without_a_freeze_mark_is_not_live(rig: Rig) -> None:
    set_schedule(rig.ctx, MORNING)
    first = run_pass(rig, "Monday.")
    rig.clock.advance(hours=22)
    second = run_pass(rig, "Tuesday.")
    rig.ctx.db.execute("UPDATE run_reports SET frozen_at = NULL")  # as a store from before 0290
    assert repo.live_run(rig.ctx.db) == repo.get_run(rig.ctx.db, second.handle)
    refused = rig.call("keep", kind="artifact", owner=first.handle, reason="R", text="late")
    assert refused["error"]["code"] == "artifact.owner_ended"
    with pytest.raises(StopIteration):
        next(r for r in read_reports(rig.ctx, first.handle) if r.text == "late")
