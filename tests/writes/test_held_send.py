"""Held sends: a drafted reply waits for the owner, then goes once on confirmation (W-48, W-49)."""

import json
from dataclasses import dataclass
from typing import Any

import pytest

from chamberlain.connectors.fake import Fault
from chamberlain.domain.grants import GrantState, Requirement
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.operations import OperationState
from chamberlain.memory.cards import CardService
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.writes import repo
from chamberlain.writes.confirm import ConfirmationApp, Response
from chamberlain.writes.held import ACTOR
from chamberlain.writes.models import GrantRow, OperationRow, Proposal
from support.write_rig import WriteRig

ORIGIN = "http://127.0.0.1:5555"
TOKEN = "t" * 43


def confirm(rig: WriteRig, grant: str, content_sha256: str) -> Response:
    """The owner's click in the viewer: the real endpoint, with token and origin."""
    app = ConfirmationApp(lambda: rig.engine().ctx, token=TOKEN, origin=ORIGIN, policy=rig.policy)
    headers = {"Host": "127.0.0.1:5555", "X-Chamberlain-Token": TOKEN, "Origin": ORIGIN}
    body = json.dumps({"content_sha256": content_sha256}).encode()
    return app.handle("POST", f"/api/grants/{grant}/confirm", headers, body)


def owner_action(rig: WriteRig, title: str = "Approve the reply to Sophie: the dates") -> str:
    cards = CardService(rig.db, rig.clock)
    card = cards.create(title="Sophie")
    view = cards.add_action(
        card.handle, None, title=title, due_at=None, snooze_until=None, for_owner=True
    )
    return view.actions[0].handle


def action_row(rig: WriteRig, action: str) -> Any:
    return rig.db.execute("SELECT * FROM actions WHERE handle = ?", (action,)).fetchone()


@dataclass(frozen=True)
class Held:
    proposal: Proposal
    grant: GrantRow
    operation: OperationRow

    @property
    def ready(self) -> bool:
        return self.proposal.ready

    @property
    def content_sha256(self) -> str:
        return self.proposal.content_sha256


def held(rig: WriteRig, action: str, **kwargs: Any) -> Held:
    proposal = rig.engine().propose_intent(rig.send(**kwargs), hold_for=action)
    assert proposal.grant is not None
    assert proposal.operation is not None
    return Held(proposal, proposal.grant, proposal.operation)


def test_w48_a_held_send_waits_for_the_owner_even_under_a_standing_grant(rig: WriteRig) -> None:
    rig.standing_grant(("friend@example.org",))
    action = owner_action(rig)
    proposal = held(rig, action)
    assert not proposal.ready
    assert proposal.proposal.requirement is Requirement.HUMAN_CONFIRMATION
    grant = proposal.grant
    assert grant.action == action
    assert grant.state is GrantState.PROPOSED
    assert proposal.operation.card == action_row(rig, action)["card"]
    with pytest.raises(ChamberlainError):
        rig.engine().do(proposal.operation.handle)
    assert rig.sent_copies() == 0
    # Proposing the same text again, without the action, does not release it either.
    again = rig.engine().propose_intent(rig.send(), card=proposal.operation.card)
    assert not again.ready


def test_w49_confirming_sends_the_exact_text_once_and_finishes_the_action(rig: WriteRig) -> None:
    action = owner_action(rig)
    proposal = held(rig, action, to=("stranger@example.org",), body="Exactly this.")
    reply = confirm(rig, proposal.grant.handle, proposal.content_sha256)
    assert reply.status == 200
    assert reply.body["send"]["result"] == "sent"
    assert reply.body["state"] == "spent"
    op = repo.get_operation(rig.db, proposal.operation.handle)
    assert op is not None
    assert op.state is OperationState.CONFIRMED
    assert op.intent_sha256 == proposal.content_sha256
    assert rig.sent_copies() == 1
    sent = [m for m in rig.fake.world.mail["mail:test"] if m.body == "Exactly this."]
    assert len(sent) == 1
    row = action_row(rig, action)
    assert row["state"] == "done"
    assert str(row["outcome"]).startswith("Sent as approved at ")
    assert str(row["outcome"]).endswith(" (local)")
    actors = {
        r["actor"]
        for r in rig.db.execute("SELECT actor FROM audit_log WHERE subject = ?", (op.handle,))
    }
    assert ACTOR in actors


def test_w49_a_second_confirm_and_a_later_do_never_send_twice(rig: WriteRig) -> None:
    action = owner_action(rig)
    proposal = held(rig, action)
    assert confirm(rig, proposal.grant.handle, proposal.content_sha256).status == 200
    second = confirm(rig, proposal.grant.handle, proposal.content_sha256)
    assert second.status == 409
    done = rig.engine().do(proposal.operation.handle)
    assert done.state is OperationState.CONFIRMED
    applied = [r for r in rig.fake.world.dispatch_log if r.applied]
    assert len(applied) == 1
    assert rig.sent_copies() == 1


def test_w49_a_send_that_fails_leaves_the_action_open_with_the_reason(rig: WriteRig) -> None:
    action = owner_action(rig)
    proposal = held(rig, action)
    rig.fake.arm(Fault.BUSY_BEFORE_APPLY)
    reply = confirm(rig, proposal.grant.handle, proposal.content_sha256)
    assert reply.body["send"]["result"] == "not_sent"
    row = action_row(rig, action)
    assert row["state"] == "open"
    notes = rig.db.execute("SELECT text FROM notes WHERE action = ?", (action,)).fetchall()
    assert any("was not sent" in str(n["text"]) for n in notes)
    assert rig.sent_copies() == 0


def test_w49_an_uncertain_send_is_left_for_reconcile_and_said_so(rig: WriteRig) -> None:
    action = owner_action(rig)
    proposal = held(rig, action)
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    reply = confirm(rig, proposal.grant.handle, proposal.content_sha256)
    assert reply.body["send"]["result"] == "uncertain"
    assert action_row(rig, action)["state"] == "open"
    notes = rig.db.execute("SELECT text FROM notes WHERE action = ?", (action,)).fetchall()
    assert any("never send it again" in str(n["text"]) for n in notes)


def test_w49_a_confirmed_one_off_send_without_an_action_still_goes(rig: WriteRig) -> None:
    proposal = rig.engine().propose_intent(rig.send(to=("stranger@example.org",)))
    assert proposal.grant is not None
    assert proposal.grant.action is None
    reply = confirm(rig, proposal.grant.handle, proposal.content_sha256)
    assert reply.body["send"]["result"] == "sent"
    assert rig.sent_copies() == 1


def test_w48_only_an_open_owner_action_can_hold_a_send(rig: WriteRig) -> None:
    cards = CardService(rig.db, rig.clock)
    card = cards.create(title="Elsewhere")
    plain = cards.add_action(card.handle, None, title="Mine", due_at=None, snooze_until=None)
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().propose_intent(rig.send(), hold_for=plain.actions[0].handle)
    assert raised.value.code is ErrorCode.REQUEST_INVALID
    action = owner_action(rig)
    with pytest.raises(ChamberlainError):
        rig.engine().propose_intent(rig.send(), hold_for=action, card=card.handle)


def test_w49_the_confirmation_page_shows_everything_that_will_be_sent(rig: WriteRig) -> None:
    from chamberlain.writes.confirm import grant_detail

    original = rig.seed_message("<orig@example.org>")
    action = owner_action(rig)
    intent = rig.send(to=("a@example.org",), subject="Re: Dates", body="Line one.\nLine two.")
    intent = intent.model_copy(
        update={"cc": ("b@example.org",), "high_importance": True, "reply_to": original}
    )
    proposal = rig.engine().propose_intent(intent, hold_for=action)
    assert proposal.grant is not None
    shown = grant_detail(rig.engine().ctx, proposal.grant.handle)["rendered"]
    for part in (
        "Confirming sends this message at once",
        "To: a@example.org",
        "Cc: b@example.org",
        "Subject: Re: Dates",
        "Importance: high",
        f"In reply to: {original}",
        "Line one.\nLine two.",
    ):
        assert part in shown
    assert IntentKind.MAIL_SEND.value == "mail.send"
