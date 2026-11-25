"""Reported mail is marked read only with the option AND the owner's confirmed standing grant.

Contract: docs/contracts/04-writes.md (W-43) and 05-grants.md (G-24). Fake world only.
"""

import pytest
from runs_rig import MAIL, Rig

from chamberlain.app.wiring import run_tick
from chamberlain.domain.grants import GrantState
from chamberlain.domain.read_state import ReadStateConfig
from chamberlain.runs import repo as runs_repo
from chamberlain.runs.models import PassKind
from chamberlain.runs.schedule import ScheduleSpec, set_schedule
from chamberlain.store.schema import READ_STATE_KEY, set_state
from chamberlain.writes import read_state, repo
from chamberlain.writes.confirm import confirm_grant
from chamberlain.writes.models import WritePolicy

MORNING = ScheduleSpec("morning", PassKind.MAIL, "08:00", "UTC")


def _grant(rig: Rig) -> list[str]:
    return read_state.propose_missing(rig.ctx.db, rig.clock, rig.ctx.services.audit, [MAIL])


@pytest.mark.parametrize("case", ["option off", "option on, no grant", "option on, grant"])
def test_reported_mail_is_marked_read_only_with_the_option_and_the_owners_grant(
    rig: Rig, case: str
) -> None:
    db = rig.ctx.db
    on = case != "option off"
    set_state(
        db, rig.clock, READ_STATE_KEY, ReadStateConfig(reporting_marks_read=on).model_dump_json()
    )
    if case.endswith("grant") and on and "no grant" not in case:
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
    rig.clock.advance(minutes=1)
    run_tick(rig.app)

    unread = {loc: rig.fake.mail.is_unread(MAIL, loc) for loc in [*handled, late]}
    if case == "option on, grant":
        assert [unread[loc] for loc in handled] == [False, False]
    else:
        assert all(unread[loc] for loc in handled), "mail stays unread without option and grant"
    assert unread[late], "mail that arrived after the report is never touched"
    waiting = repo.grants_in_states(db, (GrantState.PROPOSED,), MAIL)
    assert bool(waiting) is (case == "option on, no grant")
