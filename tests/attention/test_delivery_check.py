"""The fallback delivery check: only for a stale daily mailbox, only with the owner's grant.

Contract: docs/contracts/07-attention.md (A-7). Fake world only; the wait between polls advances
the manual clock, so nothing sleeps.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from chamberlain.app.context import SourceRouter
from chamberlain.app.core import App
from chamberlain.app.wiring import build_app, run_tick
from chamberlain.attention import delivery_check
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.grants import GrantState
from chamberlain.domain.testmark import DELIVERY_MARK, carries_delivery_mark
from chamberlain.ports.clock import ManualClock
from chamberlain.runs import sections
from chamberlain.store.migrate import stamp
from chamberlain.store.schema import MAIL_SYNC_KEY, set_state
from chamberlain.writes import delivery_check as grants
from chamberlain.writes import repo
from chamberlain.writes.confirm import confirm_grant
from chamberlain.writes.models import WritePolicy

MAIL = "mail:test"
ME = "owner@example.org"
START = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)


class Rig:
    def __init__(self, root: Path, *, daily: bool = True, test_only: bool = False) -> None:
        self.clock = ManualClock(START)
        self.fake = FakeProvider(self.clock)
        delivery_check.sleep = lambda seconds: self.clock.advance(seconds=seconds)
        self.fake.mail.add_source(MAIL)
        self.fake.mail.deliver_self_sends(MAIL)
        self.fake.world.health_override[MAIL] = HealthState.STALE
        self.app: App = build_app(
            root,
            clock=self.clock,
            sources=SourceRouter(
                mail={MAIL: self.fake.mail},
                test_only=frozenset({MAIL}) if test_only else frozenset(),
                hide_test_marked=daily,
            ),
        )
        self.ctx.db.execute(
            "INSERT INTO sources (id, kind, connector, binding_json, created_at, updated_at) "
            "VALUES (?, 'mail', 'fake', ?, '2026-01-05T06:00:00Z', '2026-01-05T06:00:00Z')",
            (MAIL, json.dumps({"address": ME})),
        )
        self.ctx.db.commit()

    @property
    def ctx(self) -> Any:
        return self.app.context

    def rows(self, sql: str, *params: object) -> list[Any]:
        return list(self.ctx.db.execute(sql, params))

    def grant(self) -> str:
        scope = grants.scope_for(self.ctx, MAIL)
        assert scope is not None
        handle = grants.propose_missing(self.ctx, scope)
        assert handle is not None
        row = repo.get_grant(self.ctx.db, handle)
        assert row is not None
        confirm_grant(self.ctx, handle, {"content_sha256": row.content_sha256}, WritePolicy())
        return handle

    def health(self) -> str:
        return str(self.rows("SELECT state FROM source_health WHERE source_id = ?", MAIL)[0][0])

    def copies(self, folder: str) -> list[str]:
        return [
            m.subject
            for m in self.fake.world.mail[MAIL]
            if m.folder_id == folder and carries_delivery_mark(m.subject)
        ]

    def sends(self) -> int:
        return len(self.rows("SELECT 1 FROM operations WHERE kind = 'mail.send'"))

    def tick(self) -> None:
        run_tick(self.app)


@pytest.fixture(autouse=True)
def restore_sleep() -> Iterator[None]:
    original = delivery_check.sleep
    yield
    delivery_check.sleep = original


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path / "data")
    try:
        yield made
    finally:
        made.app.close()


def test_stale_with_a_grant_sends_sees_it_arrive_and_moves_it_to_trash(rig: Rig) -> None:
    rig.grant()
    rig.tick()
    assert rig.health() == "ok"
    assert rig.sends() == 1
    for folder in ("f-inbox", "f-sent"):
        assert rig.copies(folder) == [], "no copy is left outside Trash"
    assert len(rig.copies("f-trash")) >= 1
    assert {r["kind"] for r in rig.rows("SELECT kind FROM operations")} >= {
        "mail.send",
        "mail.trash",
    }
    assert {r[0] for r in rig.rows("SELECT DISTINCT actor FROM audit_log")} >= {
        "system:delivery-check"
    }
    record = delivery_check.records(rig.ctx.db)[MAIL]
    assert record.result == "arrived"
    assert record.pending is None
    # The report says so.
    facts = _facts(rig)
    assert "confirmed by a delivery check at 06:00" in sections.verification(facts)


def _facts(rig: Rig) -> sections.Facts:
    now = stamp(rig.clock)
    run = SimpleNamespace(started_at=now, due_at=now)
    return sections.Facts(rig.ctx, run, ZoneInfo("UTC"))  # type: ignore[arg-type]


def test_the_probe_message_never_becomes_mail_seen(rig: Rig) -> None:
    rig.grant()
    rig.fake.world.health_override.pop(MAIL)  # healthy: the scan reads everything
    rig.tick()
    rig.fake.mail.add_message(
        MAIL, internet_message_id="<probe@x>", subject=f"{DELIVERY_MARK} {MAIL} 20260105T060000Z"
    )
    rig.fake.mail.add_message(MAIL, internet_message_id="<real@x>", subject="Real mail")
    rig.clock.advance(minutes=10)
    rig.tick()
    seen = rig.rows("SELECT message_key FROM mail_seen")
    assert [r["message_key"] for r in seen] == ["real@x"]


def test_stale_with_no_grant_proposes_once_and_never_sends(rig: Rig) -> None:
    rig.tick()
    rig.clock.advance(hours=4)
    rig.tick()
    assert rig.health() == "stale"
    assert rig.sends() == 0
    assert rig.fake.world.mail[MAIL] == []
    proposed = repo.grants_in_states(rig.ctx.db, (GrantState.PROPOSED,), MAIL)
    assert [g.scope.title for g in proposed if g.scope] == [grants.GRANT_TITLE]


def test_at_most_one_probe_per_mailbox_in_three_hours(rig: Rig) -> None:
    rig.grant()
    rig.tick()
    rig.clock.advance(hours=1)
    rig.tick()
    rig.clock.advance(hours=1, minutes=59)
    rig.tick()
    assert rig.sends() == 1
    rig.clock.advance(minutes=2)
    rig.tick()
    assert rig.sends() == 2


def test_a_message_that_does_not_arrive_keeps_the_mailbox_stale_and_the_report_says_so(
    rig: Rig,
) -> None:
    rig.fake.mail.deliver_self_sends(MAIL, False)
    rig.grant()
    started = rig.clock.now()
    rig.tick()
    assert rig.clock.now() - started <= timedelta(seconds=120 + 30)
    assert rig.health() == "stale"
    record = delivery_check.records(rig.ctx.db)[MAIL]
    assert record.result == "missing"
    assert record.pending, "kept so the next probe only cleans up"
    assert rig.copies("f-sent") == [], "the Sent copy was moved to Trash"
    assert "The delivery check also failed." in sections.access_problems(_facts(rig))


def test_a_later_probe_after_a_crash_only_cleans_up_first(rig: Rig) -> None:
    rig.fake.mail.deliver_self_sends(MAIL, False)
    rig.grant()
    rig.tick()
    stray = delivery_check.records(rig.ctx.db)[MAIL].pending
    assert stray
    rig.fake.mail.add_message(MAIL, internet_message_id="<late@x>", subject=stray)  # arrives late
    rig.clock.advance(hours=4)
    rig.fake.mail.deliver_self_sends(MAIL)
    rig.tick()
    assert rig.copies("f-inbox") == []
    assert rig.health() == "ok"


def test_a_test_only_mailbox_is_never_probed(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "data", test_only=True)
    try:
        rig.tick()
        assert rig.sends() == 0
        assert rig.fake.world.mail[MAIL] == []
        assert not repo.grants_in_states(rig.ctx.db, (GrantState.PROPOSED,))
    finally:
        rig.app.close()


def test_the_live_tests_own_app_never_probes(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "data", daily=False)
    try:
        rig.grant()
        rig.tick()
        assert rig.sends() == 0
        assert rig.health() == "stale"
    finally:
        rig.app.close()


@pytest.mark.parametrize("case", ["unavailable", "switched off"])
def test_no_probe_when_unavailable_or_switched_off(rig: Rig, case: str) -> None:
    rig.grant()
    if case == "unavailable":
        rig.fake.world.health_override[MAIL] = HealthState.UNAVAILABLE
    else:
        set_state(rig.ctx.db, rig.clock, MAIL_SYNC_KEY, json.dumps({"delivery_check": False}))
    rig.tick()
    assert rig.sends() == 0
    assert rig.health() == case.replace("switched off", "stale")
