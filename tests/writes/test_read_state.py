"""The owner's read-state option for filing: it acts only through a confirmed standing grant.

Contract: docs/contracts/05-grants.md (G-24). Fake world only.
"""

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
