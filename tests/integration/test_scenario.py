"""One end-to-end scenario over the fake provider, through the real wiring.

start, find, open, card, keep, propose, confirm with the user's words, do with a lost
response, reconcile, a scheduled tick and the sweep. Every step goes through `App.call`,
the way the CLI and the MCP face do. Contract: docs/contracts/10-targets.md (Safety).
"""

import sqlite3
from pathlib import Path

from tests.integration.conftest import MAIL, Stack, UnknownReconciles, build_stack

from chamberlain.app.wiring import run_tick
from chamberlain.connectors.fake import Fault
from chamberlain.domain.mail import FolderRole
from chamberlain.domain.operations import OperationState
from chamberlain.writes import repo
from chamberlain.writes.audit import verify
from chamberlain.writes.confirm import confirm_grant
from chamberlain.writes.models import OperationRow

FRIEND = "friend@example.org"


def sent_copies(stack: Stack, message_id: str) -> int:
    return stack.fake.mail.copies(MAIL, message_id, "f-sent")


def stored_operation(stack: Stack, handle: str) -> OperationRow:
    row = repo.get_operation(stack.app.context.db, handle)
    assert row is not None
    return row


def token_of(stack: Stack, handle: str) -> str:
    token = stored_operation(stack, handle).reconcile_token
    assert token is not None
    return token


def audit_kinds(db: sqlite3.Connection, kind: str) -> int:
    return int(db.execute("SELECT count(*) FROM audit_log WHERE kind = ?", (kind,)).fetchone()[0])


def test_the_whole_scenario_confirms_a_send_exactly_once(stack: Stack) -> None:
    stack.fake.mail.add_message(
        MAIL,
        internet_message_id="<in1@example.org>",
        subject="Dinner on Friday?",
        sender=FRIEND,
        body="Can you do 7pm?",
    )
    stack.know(FRIEND)

    # start: one work item, one exact next call
    started = stack.start()
    assert [item["kind"] for item in started["work"]] == ["mail.new_unmatched"]
    work = started["work"][0]
    assert work["next"]["tool"] == "find"
    assert work["subject"] == MAIL

    # the batch lists the mail; open only what is needed (read-only: it stays unread, P-3)
    hits = stack.ok("find", **work["next"]["args"])["hits"]
    assert len(hits) == 1
    message = hits[0]["handle"]
    opened = stack.ok("open", handle=message, part="body")
    assert "7pm" in opened["content"]["text"]
    assert stack.fake.mail.is_unread(MAIL, stack.app.context.services.refs.locate(message)[1])

    # card and keep
    card = stack.ok("card", op="create", fields={"title": "Dinner on Friday", "goal": "Reply"})
    card_handle = card["card"]["handle"]
    assert stack.ok("open", handle=card_handle)["view"]["title"] == "Dinner on Friday"
    action = stack.ok(
        "card", op="add_action", card=card_handle, action_fields={"title": "Reply about Friday"}
    )["card"]["actions"][0]["handle"]
    note = stack.ok(
        "keep", kind="note", owner=action, text="Friend asked for 7pm.", reason="Context."
    )
    assert note["handle"].startswith("note_")

    # propose a send; the known recipient needs only the user's own words
    intent = {
        "kind": "mail.send",
        "source": MAIL,
        "to": [FRIEND],
        "subject": "Re: Dinner on Friday?",
        "body": "7pm works for me.",
        "reply_to": message,
    }
    proposal = stack.ok("propose", intent=intent, card=card_handle)
    assert proposal["requirement"] == "agent_evidence"
    assert proposal["ready"] is False
    operation = proposal["operation"]
    words = stack.ok(
        "keep",
        kind="evidence",
        owner=operation,
        text="Owner: yes, tell them 7pm works.",
        reason="The owner's approval.",
    )
    approved = stack.ok(
        "propose", operation=operation, approval_evidence=words["handle"], card=card_handle
    )
    assert approved["ready"] is True

    # do with a lost response: the send applied, the answer never arrived
    stack.fake.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    done = stack.call("do", operation=operation)
    if done["ok"]:
        view = done["data"]["operation"]
    else:
        assert done["error"]["may_have_happened"] is True
        view = stack.ok("reconcile", operation=operation)["operation"]
    for _ in range(3):
        if view["state"] == "confirmed":
            break
        view = stack.ok("reconcile", operation=operation)["operation"]

    assert view["state"] == OperationState.CONFIRMED.value
    # confirmed exactly once: one dispatch, one Sent copy, a second `do` repeats nothing
    assert len(stack.fake.dispatch_log) == 1
    assert sent_copies(stack, token_of(stack, operation)) == 1
    again = stack.ok("do", operation=operation)["operation"]
    assert again["state"] == "confirmed"
    assert len(stack.fake.dispatch_log) == 1

    # the audit chain is intact and recorded the whole story
    assert verify(stack.app.context.db).ok
    assert audit_kinds(stack.app.context.db, "tool.call") >= 10

    # a scheduled pass: the clock moves past an artifact's expiry, the tick sweeps it away
    kept = stack.ok(
        "keep",
        kind="artifact",
        owner=action,
        text="scratch text for the send",
        reason="A working copy.",
        expires_in_hours=1,
    )
    stored_files = [p for p in (stack.root / "artifacts").rglob("*") if p.is_file()]
    assert stored_files
    stack.clock.advance(hours=2)
    sweeps_before = audit_kinds(stack.app.context.db, "sweep")
    ran, wake = run_tick(stack.app)
    assert ran
    assert wake in (0, 10)
    assert audit_kinds(stack.app.context.db, "sweep") == sweeps_before + 1
    assert not [p for p in (stack.root / "artifacts").rglob("*") if p.is_file()]
    assert stack.ok("open", handle=kept["handle"])["view"]["state"] == "deleted"
    gone = stack.call("open", handle=kept["handle"], part="body")
    assert gone["ok"] is False
    assert gone["error"]["code"] == "artifact.expired"
    assert verify(stack.app.context.db).ok


def propose_ready_send(stack: Stack) -> str:
    """A send to a known recipient, approved with the user's words; returns the operation."""
    stack.know(FRIEND)
    stack.start()
    intent = {
        "kind": "mail.send",
        "source": MAIL,
        "to": [FRIEND],
        "subject": "Lunch",
        "body": "Noon works.",
    }
    operation = stack.ok("propose", intent=intent)["operation"]
    words = stack.ok(
        "keep", kind="evidence", owner=operation, text="Owner: yes, send it.", reason="Approval."
    )
    assert stack.ok("propose", operation=operation, approval_evidence=words["handle"])["ready"]
    return operation


def test_an_unanswered_reconcile_keeps_the_operation_uncertain_until_it_is_read(
    tmp_path: Path,
) -> None:
    stack = build_stack(tmp_path / "data", lambda mail: UnknownReconciles(mail, 3))
    try:
        operation = propose_ready_send(stack)
        stack.fake.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
        lost = stack.call("do", operation=operation)
        assert lost["ok"] is False
        assert lost["error"]["code"] == "operation.uncertain"
        assert lost["error"]["may_have_happened"] is True
        assert stored_operation(stack, operation).state is OperationState.UNCERTAIN

        # never dispatched twice, whatever the agent does next
        repeat = stack.call("do", operation=operation)
        assert repeat["ok"] is False
        assert len(stack.fake.dispatch_log) == 1

        settled = stack.ok("reconcile", operation=operation)
        assert settled["operation"]["state"] == "confirmed"
        assert len(stack.fake.dispatch_log) == 1
        assert sent_copies(stack, token_of(stack, operation)) == 1
        assert verify(stack.app.context.db).ok
    finally:
        stack.app.close()


def test_an_unknown_recipient_needs_the_human_and_becomes_known_afterwards(stack: Stack) -> None:
    stack.start()
    intent = {
        "kind": "mail.send",
        "source": MAIL,
        "to": ["new@example.org"],
        "subject": "Hello",
        "body": "Nice to meet you.",
    }
    proposal = stack.ok("propose", intent=intent)
    assert proposal["requirement"] == "human_confirmation"
    words = stack.ok(
        "keep",
        kind="evidence",
        owner=proposal["operation"],
        text="Owner: yes.",
        reason="Approval.",
    )
    refused = stack.call(
        "propose", operation=proposal["operation"], approval_evidence=words["handle"]
    )
    assert refused["error"]["code"] == "grant.human_confirmation_required"

    confirm_grant(
        stack.app.context, proposal["grant"], {"content_sha256": proposal["content_sha256"]}
    )
    sent = stack.ok("do", operation=proposal["operation"])["operation"]
    assert sent["state"] == "confirmed"

    second = stack.ok("propose", intent={**intent, "subject": "Hello again"})
    assert second["requirement"] == "agent_evidence"


def test_the_same_scenario_through_the_thunderbird_connector(thunderbird_stack: Stack) -> None:
    """Start to confirmed over the real add-on under Node, a lost answer included.

    The send is matched by content and the token header (R1), the copy sits in Sent once.
    """
    rig = thunderbird_stack.fake
    rig.seed(internet_message_id="<in1@example.org>", subject="Dinner on Friday?", unread=True)
    thunderbird_stack.know(FRIEND)

    started = thunderbird_stack.start()
    assert [item["kind"] for item in started["work"]] == ["mail.new_unmatched"]
    hits = thunderbird_stack.ok("find", scope="mail", unread=True)["hits"]
    message = hits[0]["handle"]
    opened = thunderbird_stack.ok("open", handle=message, part="body")
    assert "Body text." in opened["content"]["text"]

    intent = {
        "kind": "mail.send",
        "source": MAIL,
        "to": [FRIEND],
        "subject": "Re: Dinner on Friday?",
        "body": "7pm works for me.",
    }
    operation = thunderbird_stack.ok("propose", intent=intent)["operation"]
    words = thunderbird_stack.ok(
        "keep", kind="evidence", owner=operation, text="Owner: yes, send it.", reason="Approval."
    )
    thunderbird_stack.ok("propose", operation=operation, approval_evidence=words["handle"])

    assert rig.inject(Fault.LOSE_RESPONSE_AFTER_APPLY)
    done = thunderbird_stack.call("do", operation=operation)
    view = done["data"]["operation"] if done["ok"] else None
    for _ in range(3):
        if view is not None and view["state"] == "confirmed":
            break
        view = thunderbird_stack.ok("reconcile", operation=operation)["operation"]
    assert view is not None
    assert view["state"] == "confirmed"
    token = token_of(thunderbird_stack, operation)
    assert rig.copies(token, rig.folder_id(FolderRole.SENT)) == 1
    assert verify(thunderbird_stack.app.context.db).ok
