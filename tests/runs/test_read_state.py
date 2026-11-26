"""Reported mail is marked read only with the option AND the owner's confirmed standing grant.

Contract: docs/contracts/04-writes.md (W-43) and 05-grants.md (G-24). Fake world only.
"""

from collections.abc import Mapping

import pytest
from runs_rig import MAIL, Rig

from chamberlain.app.wiring import run_tick
from chamberlain.domain.grants import GrantState
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.operations import DispatchResult, Prepared
from chamberlain.domain.read_state import ReadStateConfig
from chamberlain.runs import repo as runs_repo
from chamberlain.runs.models import PassKind
from chamberlain.runs.schedule import ScheduleSpec, set_schedule
from chamberlain.store.migrate import stamp
from chamberlain.store.schema import READ_STATE_KEY, set_state
from chamberlain.writes import read_state, repo
from chamberlain.writes.confirm import confirm_grant
from chamberlain.writes.models import WritePolicy

MORNING = ScheduleSpec("morning", PassKind.MAIL, "08:00", "UTC")


def _grant(rig: Rig) -> list[str]:
    return read_state.propose_missing(
        rig.ctx.db, rig.clock, rig.ctx.services.audit, [MAIL], policy=rig.ctx.write_policy
    )


@pytest.mark.parametrize(
    "case", ["option off", "option on, no grant", "option on, grant", "covered, partial result"]
)
def test_reported_mail_is_marked_read_only_with_the_option_and_the_owners_grant(
    rig: Rig, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = rig.ctx.db
    on = case != "option off"
    set_state(
        db, rig.clock, READ_STATE_KEY, ReadStateConfig(reporting_marks_read=on).model_dump_json()
    )
    covered = case in ("option on, grant", "covered, partial result")
    if covered:
        (handle,) = _grant(rig)
        row = repo.get_grant(db, handle)
        assert row is not None
        confirm_grant(rig.ctx, handle, {"content_sha256": row.content_sha256}, WritePolicy())

    handled = [
        rig.fake.mail.add_message(MAIL, internet_message_id=f"<seen{n}@x>") for n in range(2)
    ]
    set_schedule(rig.ctx, MORNING)
    rig.clock.advance(hours=2)  # 08:00 on the pass day
    run_tick(rig.app)
    run = runs_repo.recent_runs(db, 1)[0]
    started = rig.start(purpose="scheduled", scheduled_day=run.day)
    events = [w["event"] for w in started["data"]["work"] if w["kind"] == "mail.new_unmatched"]
    assert events
    assert rig.call("next", handled=events)["ok"]  # the pass triaged what it was shown
    rig.clock.advance(minutes=1)
    late = rig.fake.mail.add_message(MAIL, internet_message_id="<late@x>")  # arrives after
    assert rig.call(
        "keep", kind="artifact", owner=run.handle, reason="Morning report", text="2 new."
    )["ok"]
    if case == "covered, partial result":
        original = rig.fake.mail.dispatch

        def dispatch(
            prepared: Prepared, key: str, attachments: Mapping[str, bytes]
        ) -> DispatchResult:
            rig.fake.world.mail[MAIL] = [
                m for m in rig.fake.world.mail[MAIL] if m.internet_message_id != "<seen1@x>"
            ]
            return original(prepared, key, attachments)

        monkeypatch.setattr(rig.fake.mail, "dispatch", dispatch)
    rig.clock.advance(minutes=1)
    # Shown to the same pass after its report was kept, before the tick that settles it.
    (session,) = db.execute("SELECT session FROM runs WHERE handle = ?", (run.handle,)).fetchone()
    seen_late = rig.ctx.services.refs.remember(HandleKind.MESSAGE, MAIL, late)
    db.execute(
        "INSERT INTO mail_seen (source_id, locator, handle, received_at, first_seen_at, triaged, "
        "delivered_session) VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT (source_id, locator) DO "
        "UPDATE SET first_seen_at = excluded.first_seen_at, triaged = 1, "
        "delivered_session = excluded.delivered_session",
        (MAIL, late, seen_late, stamp(rig.clock), stamp(rig.clock), session),
    )
    db.commit()
    run_tick(rig.app)
    present = handled[:1] if case == "covered, partial result" else handled

    unread = {loc: rig.fake.mail.is_unread(MAIL, loc) for loc in [*present, late]}
    if covered:
        assert not any(unread[loc] for loc in present)
    else:
        assert all(unread[loc] for loc in handled), "mail stays unread without option and grant"
    stamped = {
        r["locator"]
        for r in db.execute("SELECT locator FROM mail_seen WHERE read_marked_at IS NOT NULL")
    }
    assert stamped == (set(present) if covered else set())
    assert unread[late], "mail that arrived after the report is never touched"
    waiting = repo.grants_in_states(db, (GrantState.PROPOSED,), MAIL)
    assert bool(waiting) is (case == "option on, no grant")
