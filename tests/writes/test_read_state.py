"""The owner's read-state option for filing: it acts only through a confirmed standing grant.

Contract: docs/contracts/05-grants.md (G-24). Fake world only.
"""

import pytest

from chamberlain.domain.grants import GrantState
from chamberlain.domain.intents import MailMove
from chamberlain.domain.read_state import ReadStateConfig
from chamberlain.store.schema import READ_STATE_KEY, set_state
from chamberlain.writes import read_state, repo
from support.write_rig import MAIL, WriteRig


def _option(rig: WriteRig, **options: bool) -> None:
    set_state(rig.db, rig.clock, READ_STATE_KEY, ReadStateConfig(**options).model_dump_json())


def _file(rig: WriteRig, message_id: str) -> tuple[str, str]:
    """Propose filing one message; return its locator and whether the stored move marks read."""
    handle = rig.seed_message(message_id)
    proposal = rig.engine().propose_intent(
        MailMove(source=MAIL, messages=(handle,), to_folder="f-archive")
    )
    assert proposal.operation is not None
    stored = rig.engine().intent_of(proposal.operation)
    assert isinstance(stored, MailMove)
    return message_id, str(stored.mark_read)


def _proposed(rig: WriteRig) -> list[str]:
    grants = repo.grants_in_states(rig.db, (GrantState.PROPOSED,), MAIL)
    return [g.handle for g in grants if g.proposed_by == read_state.ACTOR]


def test_filing_marks_read_only_with_the_option_and_a_confirmed_grant(rig: WriteRig) -> None:
    # Option off (the default): filing never changes read state and nothing is proposed.
    assert _file(rig, "<a@x>")[1] == "False"
    assert _proposed(rig) == []

    # Option on, no grant yet: still unread, and the harness proposes the grant once.
    _option(rig, filing_marks_read=True)
    assert _file(rig, "<b@x>")[1] == "False"
    assert _file(rig, "<c@x>")[1] == "False"
    (waiting,) = _proposed(rig)
    grant = repo.get_grant(rig.db, waiting)
    assert grant is not None
    assert grant.scope is not None
    assert grant.scope.allow_mark_read
    assert grant.requirement.value == "human_confirmation"

    # The owner's click confirms it: now a filing move marks the message read.
    from chamberlain.writes.confirm import confirm_grant

    confirm_grant(rig.engine().ctx, waiting, {"content_sha256": grant.content_sha256}, rig.policy)
    assert _file(rig, "<d@x>")[1] == "True"
    handle = rig.seed_message("<e@x>")
    proposal = rig.engine().propose_intent(
        MailMove(source=MAIL, messages=(handle,), to_folder="f-archive")
    )
    approved = rig.agent_approves(proposal, "Yes, file it.")
    assert approved.operation is not None
    rig.engine().do(approved.operation.handle)
    moved = [m for m in rig.fake.world.mail[MAIL] if m.internet_message_id == "<e@x>"]
    assert moved
    assert moved[0].folder_id == "f-archive"
    assert not moved[0].unread
    untouched = [m for m in rig.fake.world.mail[MAIL] if m.internet_message_id == "<b@x>"]
    assert untouched
    assert untouched[0].unread


@pytest.mark.parametrize("case", ["revoked", "expired", "suspended", "wrong card", "other covers"])
def test_engine_run_uses_only_the_covering_pinned_grant(rig: WriteRig, case: str) -> None:
    from chamberlain.domain.intents import MailMarkRead
    from chamberlain.writes.confirm import confirm_grant

    handle = rig.seed_message()
    engine = rig.engine()

    def grant(**fields: object) -> str:
        scope = read_state.scope_for(MAIL).model_copy(update=fields)
        proposal = engine.propose_standing(scope)
        assert proposal.grant
        g = confirm_grant(
            engine.ctx,
            proposal.grant.handle,
            {"content_sha256": proposal.content_sha256},
            rig.policy,
        )
        return g.handle

    pinned = grant(
        **(
            {"card": "card_" + "1" * 16}
            if case == "wrong card"
            else {"folders": frozenset({"f-archive"})}
            if case == "other covers"
            else {}
        )
    )
    if case in ("revoked", "expired", "suspended"):
        rig.db.execute("UPDATE grants SET state=? WHERE handle=?", (case, pinned))
    covering = None
    if case == "other covers":
        covering = grant(title="General read permission")
        assert covering != pinned
    intent = MailMarkRead(source=MAIL, messages=(handle,))
    assert engine.run(intent, actor=read_state.ACTOR, grant=pinned) is None
    assert rig.fake.world.mail[MAIL][0].unread
    assert rig.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    if case == "other covers":
        assert covering is not None
        selected = read_state.active_grant(engine.ctx, intent)
        assert selected
        assert selected.handle == covering
        assert engine.run(intent, actor=read_state.ACTOR, grant=covering)
        assert not rig.fake.world.mail[MAIL][0].unread
