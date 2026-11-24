"""Invitations end to end on the fake world: send, change, cancel and answer (G-12b, W-33)."""

from datetime import UTC, datetime

import pytest

from chamberlain.domain.calendar import EventFields, EventStatus, Response
from chamberlain.domain.grants import StandingScope
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarRespond,
    CalendarUpdate,
    IntentKind,
)
from chamberlain.domain.operations import OperationState
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.testing.write_rig import (
    CALENDAR,
    CALENDAR_B,
    CALENDAR_PLAIN,
    OWNER,
    OWNER_B,
    WriteRig,
)
from chamberlain.writes.authorise import authorise, requirement_needed
from chamberlain.writes.confirm import confirm_grant

START = datetime(2026, 2, 1, 12, tzinfo=UTC)
END = datetime(2026, 2, 1, 13, tzinfo=UTC)
OUTSIDER = "stranger@elsewhere.example"


def fields(*attendees: str, title: str = "Lunch", start: datetime = START) -> EventFields:
    return EventFields(
        title=title,
        start=start,
        end=start + (END - START),
        attendees=attendees,
    )


def invite(*attendees: str, source: str = CALENDAR) -> CalendarCreate:
    return CalendarCreate(source=source, event=fields(*attendees))


@pytest.fixture
def owned(rig: WriteRig) -> WriteRig:
    rig.own(CALENDAR, OWNER)
    rig.own(CALENDAR_B, OWNER_B)
    return rig


def send_invitation(rig: WriteRig, *attendees: str) -> str:
    """Propose, approve as the agent, do. Returns the uid of the organiser's event."""
    proposal = rig.engine().propose_intent(invite(*attendees))
    assert proposal.requirement.value == "agent_evidence"
    approved = rig.agent_approves(proposal, "Yes, invite me.")
    assert approved.ready
    assert approved.operation is not None
    done = rig.engine().do(approved.operation.handle)
    assert done.state is OperationState.CONFIRMED
    assert done.reconcile_token is not None
    return done.reconcile_token


def handle_on(rig: WriteRig, source: str, uid: str) -> str:
    copy = rig.fake.calendar.copy_for(source, uid)
    assert copy is not None
    return rig.refs.remember(HandleKind.CALENDAR_EVENT, source, copy.id)


def test_g12b_an_invitation_to_the_owners_own_address_needs_only_agent_evidence(
    owned: WriteRig,
) -> None:
    proposal = owned.engine().propose_intent(invite(OWNER_B))
    assert proposal.requirement.value == "agent_evidence"


def test_g12b_an_unknown_or_mixed_attendee_list_needs_the_human(owned: WriteRig) -> None:
    for attendees in ((OUTSIDER,), (OWNER_B, OUTSIDER)):
        proposal = owned.engine().propose_intent(invite(*attendees))
        assert proposal.requirement.value == "human_confirmation"


def test_g12b_authorised_senders_owner_addresses_count_as_the_owners(rig: WriteRig) -> None:
    other = "me.third@example.net"
    first = rig.engine().propose_intent(invite(other))
    assert first.requirement.value == "human_confirmation"
    rig.db.execute(
        "INSERT INTO system_state (key, value, updated_at) VALUES ('app.authorised_senders', ?, ?)",
        ('{"owner": ["' + other + '"]}', "2026-01-05T09:00:00Z"),
    )
    second = rig.engine().propose_intent(invite(other))
    assert second.requirement.value == "agent_evidence"


def test_w33_the_invitation_reaches_the_invitees_calendar_and_the_answer_comes_back(
    owned: WriteRig,
) -> None:
    uid = send_invitation(owned, OWNER_B)
    mine = owned.fake.calendar.copy_for(CALENDAR, uid)
    theirs = owned.fake.calendar.copy_for(CALENDAR_B, uid)
    assert mine is not None
    assert theirs is not None
    assert mine.fields.attendees == (OWNER_B,)
    assert theirs.organiser == OWNER
    assert theirs.my_response is Response.NEEDS_ACTION
    # The organiser sees the attendee list on the stored event.
    seen = owned.fake.calendar.read(CALENDAR, mine.id)
    assert seen.fields.attendees == (OWNER_B,)
    assert [(p.address, p.response) for p in seen.participants] == [
        (OWNER_B, Response.NEEDS_ACTION)
    ]

    reply = CalendarRespond(
        source=CALENDAR_B,
        event_ref=handle_on(owned, CALENDAR_B, uid),
        expected_etag=owned.fake.calendar.read(CALENDAR_B, theirs.id).etag,
        response="accepted",
        organiser="spoofed@example.org",
    )
    proposal = owned.engine().propose_intent(reply)
    assert proposal.requirement.value == "agent_evidence"
    assert "Sent to the organiser: " + OWNER in proposal.preview
    approved = owned.agent_approves(proposal, "Yes, accept it.")
    assert approved.operation is not None
    assert owned.engine().do(approved.operation.handle).state is OperationState.CONFIRMED

    assert owned.fake.calendar.copy_for(CALENDAR_B, uid).my_response is Response.ACCEPTED  # type: ignore[union-attr]
    after = owned.fake.calendar.read(CALENDAR, mine.id)
    assert [(p.address, p.response) for p in after.participants] == [(OWNER_B, Response.ACCEPTED)]


def test_w33_changing_the_time_resets_answers_and_cancelling_removes_the_copy(
    owned: WriteRig,
) -> None:
    uid = send_invitation(owned, OWNER_B)
    mine = owned.fake.calendar.copy_for(CALENDAR, uid)
    theirs = owned.fake.calendar.copy_for(CALENDAR_B, uid)
    assert mine is not None
    assert theirs is not None
    theirs.my_response = Response.ACCEPTED
    mine.participants[OWNER_B] = Response.ACCEPTED

    event_ref = handle_on(owned, CALENDAR, uid)
    later = fields(OWNER_B, start=datetime(2026, 2, 1, 15, tzinfo=UTC))
    update = CalendarUpdate(
        source=CALENDAR,
        event_ref=event_ref,
        expected_etag=owned.fake.calendar.read(CALENDAR, mine.id).etag,
        event=later,
    )
    proposal = owned.engine().propose_intent(update)
    assert proposal.requirement.value == "agent_evidence"
    assert "Also told of the change: " + OWNER_B in proposal.preview
    approved = owned.agent_approves(proposal, "Yes, move it.")
    assert approved.operation is not None
    assert owned.engine().do(approved.operation.handle).state is OperationState.CONFIRMED
    assert theirs.fields.start == later.start
    assert theirs.my_response is Response.NEEDS_ACTION

    delete = CalendarDelete(
        source=CALENDAR,
        event_ref=event_ref,
        expected_etag=owned.fake.calendar.read(CALENDAR, mine.id).etag,
    )
    proposal = owned.engine().propose_intent(delete)
    assert "Cancellation sent to: " + OWNER_B in proposal.preview
    approved = owned.agent_approves(proposal, "Yes, cancel it.")
    assert approved.operation is not None
    assert owned.engine().do(approved.operation.handle).state is OperationState.CONFIRMED
    assert mine.status is EventStatus.CANCELLED
    assert theirs.status is EventStatus.CANCELLED


def test_w33_removing_an_attendee_cancels_that_persons_copy(owned: WriteRig) -> None:
    uid = send_invitation(owned, OWNER_B)
    mine = owned.fake.calendar.copy_for(CALENDAR, uid)
    theirs = owned.fake.calendar.copy_for(CALENDAR_B, uid)
    assert mine is not None
    assert theirs is not None
    update = CalendarUpdate(
        source=CALENDAR,
        event_ref=handle_on(owned, CALENDAR, uid),
        expected_etag=owned.fake.calendar.read(CALENDAR, mine.id).etag,
        event=fields(),
    )
    proposal = owned.engine().propose_intent(update)
    assert proposal.requirement.value == "agent_evidence"
    approved = owned.agent_approves(proposal, "Yes, remove them.")
    assert approved.operation is not None
    owned.engine().do(approved.operation.handle)
    assert theirs.status is EventStatus.CANCELLED


def test_w33_an_invitation_to_an_outsider_waits_for_the_human_and_is_recorded_undelivered(
    owned: WriteRig,
) -> None:
    proposal = owned.engine().propose_intent(invite(OUTSIDER))
    assert proposal.requirement.value == "human_confirmation"
    owned.human_approves(proposal)
    assert proposal.operation is not None
    done = owned.engine().do(proposal.operation.handle)
    assert done.state is OperationState.CONFIRMED
    assert owned.fake.calendar.undelivered(CALENDAR) == [(done.reconcile_token, OUTSIDER)]


def test_w33_only_an_invitation_can_be_answered(owned: WriteRig) -> None:
    locator = owned.fake.calendar.add_event(CALENDAR, fields())
    handle = owned.refs.remember(HandleKind.CALENDAR_EVENT, CALENDAR, locator)
    reply = CalendarRespond(
        source=CALENDAR,
        event_ref=handle,
        expected_etag=owned.fake.calendar.read(CALENDAR, locator).etag,
        response="declined",
    )
    with pytest.raises(ChamberlainError) as raised:
        owned.engine().propose_intent(reply)
    assert raised.value.code is ErrorCode.REQUEST_INVALID


def test_w33_the_invitee_cannot_change_or_cancel_someone_elses_invitation(
    owned: WriteRig,
) -> None:
    uid = send_invitation(owned, OWNER_B)
    theirs = owned.fake.calendar.copy_for(CALENDAR_B, uid)
    assert theirs is not None
    ref = handle_on(owned, CALENDAR_B, uid)
    etag = owned.fake.calendar.read(CALENDAR_B, theirs.id).etag
    for intent in (
        CalendarDelete(source=CALENDAR_B, event_ref=ref, expected_etag=etag),
        CalendarUpdate(source=CALENDAR_B, event_ref=ref, expected_etag=etag, event=fields()),
    ):
        with pytest.raises(ChamberlainError) as raised:
            owned.engine().propose_intent(intent)
        assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
        assert raised.value.detail == "invitee-copy"


def test_w33_a_source_that_cannot_invite_refuses_at_propose(owned: WriteRig) -> None:
    with pytest.raises(ChamberlainError) as raised:
        owned.engine().propose_intent(invite(OWNER_B, source=CALENDAR_PLAIN))
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert raised.value.detail == "invitations-not-supported"
    # Without attendees the same source writes as before.
    plain = owned.engine().propose_intent(invite(source=CALENDAR_PLAIN))
    assert plain.requirement.value == "agent_evidence"


def test_g12b_answering_an_unknown_organiser_needs_the_human_and_is_recorded(
    owned: WriteRig,
) -> None:
    locator = owned.fake.calendar.add_event(
        CALENDAR,
        fields(OUTSIDER),
        organiser=OUTSIDER,
        my_response=Response.NEEDS_ACTION,
    )
    ref = owned.refs.remember(HandleKind.CALENDAR_EVENT, CALENDAR, locator)
    reply = CalendarRespond(
        source=CALENDAR,
        event_ref=ref,
        expected_etag=owned.fake.calendar.read(CALENDAR, locator).etag,
        response="tentative",
    )
    proposal = owned.engine().propose_intent(reply)
    assert proposal.requirement.value == "human_confirmation"
    owned.human_approves(proposal)
    assert proposal.operation is not None
    assert owned.engine().do(proposal.operation.handle).state is OperationState.CONFIRMED
    copy = owned.fake.calendar.read(CALENDAR, locator)
    assert copy.my_response is Response.TENTATIVE
    assert owned.fake.calendar.responses_sent(CALENDAR)[0][1:] == (OUTSIDER, Response.TENTATIVE)


def _standing(owned: WriteRig, kinds: set[IntentKind], recipients: tuple[str, ...]) -> None:
    scope = StandingScope(
        source=CALENDAR,
        intent_kinds=frozenset(kinds),
        title="Invite my other calendar",
        recipients=frozenset(recipients),
    )
    proposal = owned.engine().propose_standing(scope)
    assert proposal.grant is not None
    confirm_grant(
        owned.engine().ctx,
        proposal.grant.handle,
        {"content_sha256": proposal.content_sha256},
        owned.policy,
    )


def test_w22_a_standing_grant_covers_invitations_inside_its_exact_recipients_only(
    owned: WriteRig,
) -> None:
    _standing(owned, {IntentKind.CALENDAR_CREATE}, (OWNER_B,))
    inside = owned.engine().propose_intent(invite(OWNER_B))
    assert inside.ready
    outside = owned.engine().propose_intent(invite(OWNER_B, OUTSIDER))
    assert not outside.ready
    assert outside.requirement.value == "human_confirmation"


def test_w22_a_domain_in_a_standing_grant_never_covers_invitations(owned: WriteRig) -> None:
    scope = StandingScope(
        source=CALENDAR,
        intent_kinds=frozenset({IntentKind.CALENDAR_CREATE}),
        title="Invite anyone at example.org",
        recipient_domains=frozenset({"example.org"}),
    )
    proposal = owned.engine().propose_standing(scope)
    assert proposal.grant is not None
    confirm_grant(
        owned.engine().ctx,
        proposal.grant.handle,
        {"content_sha256": proposal.content_sha256},
        owned.policy,
    )
    assert not owned.engine().propose_intent(invite(OWNER_B)).ready


def test_w22_a_standing_grant_covers_answers_to_its_exact_organisers(owned: WriteRig) -> None:
    _standing(owned, {IntentKind.CALENDAR_RESPOND}, (OUTSIDER,))
    locator = owned.fake.calendar.add_event(
        CALENDAR, fields(OUTSIDER), organiser=OUTSIDER, my_response=Response.NEEDS_ACTION
    )
    ref = owned.refs.remember(HandleKind.CALENDAR_EVENT, CALENDAR, locator)
    etag = owned.fake.calendar.read(CALENDAR, locator).etag
    covered = CalendarRespond(
        source=CALENDAR, event_ref=ref, expected_etag=etag, response="accepted"
    )
    assert owned.engine().propose_intent(covered).ready
    other = owned.fake.calendar.add_event(
        CALENDAR,
        fields("someone@else.example", title="Other"),
        organiser="someone@else.example",
        my_response=Response.NEEDS_ACTION,
    )
    ref2 = owned.refs.remember(HandleKind.CALENDAR_EVENT, CALENDAR, other)
    etag2 = owned.fake.calendar.read(CALENDAR, other).etag
    uncovered = CalendarRespond(
        source=CALENDAR, event_ref=ref2, expected_etag=etag2, response="accepted"
    )
    assert not owned.engine().propose_intent(uncovered).ready


def test_w22_a_standing_grant_never_covers_update_or_delete(owned: WriteRig) -> None:
    _standing(owned, {IntentKind.CALENDAR_UPDATE, IntentKind.CALENDAR_DELETE}, (OWNER_B,))
    uid = send_invitation(owned, OWNER_B)
    mine = owned.fake.calendar.copy_for(CALENDAR, uid)
    assert mine is not None
    etag = owned.fake.calendar.read(CALENDAR, mine.id).etag
    ref = handle_on(owned, CALENDAR, uid)
    delete = CalendarDelete(source=CALENDAR, event_ref=ref, expected_etag=etag)
    assert not owned.engine().propose_intent(delete).ready


def test_w2_an_agent_supplied_organiser_is_replaced_before_hashing(owned: WriteRig) -> None:
    uid = send_invitation(owned, OWNER_B)
    theirs = owned.fake.calendar.copy_for(CALENDAR_B, uid)
    assert theirs is not None
    ref = handle_on(owned, CALENDAR_B, uid)
    etag = owned.fake.calendar.read(CALENDAR_B, theirs.id).etag
    honest = CalendarRespond(
        source=CALENDAR_B, event_ref=ref, expected_etag=etag, response="declined"
    )
    lying = honest.model_copy(update={"organiser": OUTSIDER})
    first = owned.engine().propose_intent(honest)
    second = owned.engine().propose_intent(lying)
    assert first.content_sha256 == second.content_sha256


def test_g12b_requirement_is_a_pure_function_of_the_facts(owned: WriteRig) -> None:
    """`authorise` stays pure: the same facts give the same answer, no store involved."""
    from chamberlain.writes.authorise import OperationFacts, Usage
    from chamberlain.writes.models import WritePolicy

    intent = invite(OWNER_B)
    usage = Usage(owner_addresses=frozenset({OWNER_B}))
    assert requirement_needed(intent, usage, WritePolicy()).value == "agent_evidence"
    assert requirement_needed(intent, Usage(), WritePolicy()).value == "human_confirmation"
    decision = authorise(intent, OperationFacts("op_x", "0" * 64), [], usage, WritePolicy(), START)
    assert decision.__class__.__name__ == "Deny"


def test_w34_a_standing_answer_grant_needs_a_source_that_can_answer(owned: WriteRig) -> None:
    scope = StandingScope(
        source=CALENDAR_PLAIN,
        intent_kinds=frozenset({IntentKind.CALENDAR_RESPOND}),
        title="Answer",
        recipients=frozenset({OUTSIDER}),
    )
    with pytest.raises(ChamberlainError) as raised:
        owned.engine().propose_standing(scope)
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
