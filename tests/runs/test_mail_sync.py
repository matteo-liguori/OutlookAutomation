"""Folders are synced and waited for before mail is read; one that will not sync is never silent.

Contract: docs/contracts/07-attention.md (A-24 to A-27) and 08-ports.md (P-20 to P-22). The fake
provider models a client whose local index lags (`unsynced=True` mail is hidden until its folder
syncs) and a folder whose sync never finishes (`stick_folder`).
"""

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from runs_rig import MAIL, Rig

from chamberlain.app.wiring import run_tick
from chamberlain.attention import repo as attention_repo
from chamberlain.attention import watermark
from chamberlain.attention.handlers import source_checks
from chamberlain.runs import sections
from chamberlain.runs.models import PassKind, RunRow, RunState
from chamberlain.runs.schedule import ScheduleSpec, set_schedule

MORNING = ScheduleSpec("morning", PassKind.MAIL, "08:00", "UTC", presync_minutes=5)
ARCHIVE = "f-archive"


def at(rig: Rig, hour: int, minute: int) -> None:
    """Move the clock to today's `hour:minute` UTC (never backwards)."""
    target = rig.clock.now().replace(hour=hour, minute=minute, second=0)
    assert target >= rig.clock.now()
    rig.clock.set(target)


def seen(rig: Rig) -> set[str]:
    return {r["message_key"] for r in rig.rows("SELECT message_key FROM mail_seen")}


def syncs(rig: Rig) -> int:
    return len(rig.fake.world.sync_log)


def sync_events(rig: Rig) -> list[Any]:
    return rig.rows(
        "SELECT * FROM events WHERE kind = 'source.stale' AND dedupe_key LIKE '%:sync' "
        "AND state IN ('new', 'delivered')"
    )


def test_a_scheduled_pass_is_synced_ahead_and_finds_fresh_folders(rig: Rig) -> None:
    set_schedule(rig.ctx, MORNING)
    at(rig, 7, 50)
    run_tick(rig.app)
    first = syncs(rig)
    assert first == 1, "the first scan syncs every folder it reads before reading"

    at(rig, 7, 53)
    rig.fake.mail.add_message(
        MAIL,
        folder_id=ARCHIVE,
        internet_message_id="<late@x>",
        date=rig.clock.now(),
        unsynced=True,
    )
    at(rig, 7, 54)
    run_tick(rig.app)
    assert syncs(rig) == first, "synced four minutes ago: nothing is stale, nothing asked"
    assert "late@x" not in seen(rig), "Thunderbird's index lags, so the mail is not there yet"

    at(rig, 7, 56)  # five minutes before the pass
    run_tick(rig.app)
    assert syncs(rig) == first + 1
    assert "late@x" in seen(rig), "the pre-sync made the scan find it"

    at(rig, 8, 0)
    run_tick(rig.app)
    started = rig.start(purpose="scheduled", scheduled_day="2026-01-05")
    kinds = [item["kind"] for item in started["data"]["work"]]
    assert "schedule.pass_due" in kinds
    assert "mail.new_unmatched" in kinds
    assert "source.stale" not in kinds
    assert syncs(rig) == first + 1, "folders synced four minutes ago are fresh at the pass"


def test_folders_synced_too_long_ago_are_synced_again_when_the_agent_starts(rig: Rig) -> None:
    run_tick(rig.app)
    first = syncs(rig)
    rig.clock.advance(minutes=40)
    rig.fake.mail.add_message(MAIL, folder_id=ARCHIVE, internet_message_id="<new@x>", unsynced=True)

    started = rig.start()
    assert started["ok"]
    assert syncs(rig) == first + 1
    assert "new@x" in seen(rig), "the agent starts after a sync, not before"
    complete = attention_repo.sync_state(rig.ctx.db, MAIL)
    assert complete is not None
    assert complete[0] == rig.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    rig.clock.advance(minutes=9)
    rig.start()
    assert syncs(rig) == first + 1, "inside the freshness window nothing is asked again"


def test_a_folder_that_will_not_sync_is_reported_waited_for_once_and_retried(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Small pages, so the scan's best-effort look back cannot reach the stuck folder's mail:
    # only the catch-up read of that folder can.
    monkeypatch.setattr(watermark, "MAIL_PAGE_LIMIT", 3)
    for n in range(8):
        rig.fake.mail.add_message(
            MAIL,
            internet_message_id=f"<filler{n}@x>",
            date=rig.clock.now() + timedelta(minutes=40 + 10 * n),
            unread=False,
        )
    rig.ctx.db.execute(
        "INSERT INTO sources (id, kind, connector, created_at, updated_at) "
        "VALUES (?, 'mail', 'fake', '2026-01-05T06:00:00Z', '2026-01-05T06:00:00Z')",
        (MAIL,),
    )
    run_tick(rig.app)
    rig.fake.mail.stick_folder(MAIL, ARCHIVE)
    stuck_at = rig.clock.now() + timedelta(minutes=30)
    rig.fake.mail.add_message(
        MAIL,
        folder_id=ARCHIVE,
        internet_message_id="<lost@x>",
        date=stuck_at - timedelta(minutes=10),
        unsynced=True,
    )

    waited: list[float] = []
    for _ in range(4):  # the folder stays stuck for hours
        rig.clock.advance(minutes=45)
        before = rig.clock.now()
        run_tick(rig.app)
        waited.append((rig.clock.now() - before).total_seconds())
    assert waited[0] < 200, "the first try is given up at the time limit, not waited for ever"
    assert max(waited[1:]) <= 30, "a folder that already failed costs one short wait per tick"
    assert "lost@x" not in seen(rig)

    events = sync_events(rig)
    assert len(events) == 1
    assert "1 folder" in events[0]["summary"]
    assert "archive" in events[0]["summary"], "named by role: folder names are remote text"
    check = next(c for c in source_checks(rig.ctx) if c.id == "sources.sync")
    assert check.status == "warn"
    run = RunRow(
        "run_x", "morning", "2026-01-05", RunState.RUNNING, "2026-01-05T08:00:00Z", None, None, None
    )
    report = sections.verification(sections.Facts(rig.ctx, run, ZoneInfo("UTC")))
    assert "Partially verified" in report
    assert "did not sync" in report

    rig.fake.mail.stick_folder(MAIL, ARCHIVE, stuck=False)
    rig.clock.advance(minutes=1)
    run_tick(rig.app)
    assert "lost@x" in seen(rig), "mail that reached the folder while it was stuck is not lost"
    assert not sync_events(rig), "the event ends once the folder syncs"
    assert next(c for c in source_checks(rig.ctx) if c.id == "sources.sync").status == "ok"
