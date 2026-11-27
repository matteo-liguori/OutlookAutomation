"""Mail scenarios: send, arrive, reply, draft, attach, mark, file, trash, find, repeat, refuse.

Contract: docs/live-test.md (Scenarios). Every action goes through the tools. Each scenario
sends only to owned addresses and registers its own cleanup first, so a failure part-way
still removes what exists.
"""

from typing import Any

from chamberlain.domain.mail import FolderRole, folded_message_id
from chamberlain.livetest.authority import scratch_folder
from chamberlain.livetest.lab import JSON, Lab, ScenarioFailure, ScenarioSkip, text_of
from chamberlain.livetest.registry import Scope, Target, scenario
from chamberlain.writes import repo

NOT_OWNED = "someone@example.invalid"
"""A reserved address no one owns. A send to it must be refused before anything is sent."""


def body(lab: Lab, text: str) -> str:
    return f"{lab.marker}\n\n{text}\n\nThis is an automated test message and is safe to delete."


def _own_mail(lab: Lab, source: str, subject: str) -> None:
    """Register the cleanup of everything in `source` whose subject contains `subject`."""
    lab.defer(f"remove marked mail in {source}", lambda: lab.remove_mail(source, subject))


def _self_message(lab: Lab, t: Target, name: str) -> tuple[str, str, JSON]:
    """Send a marked mail to the mailbox's own address and wait for it. Returns the subject,
    the address and the inbox hit."""
    address = lab.config.address_of(t.source)
    subject = lab.subject(f"{name} {t.tag}")
    _own_mail(lab, t.source, subject)
    lab.send(t.source, address, subject, body(lab, name))
    return subject, address, lab.arrival(t.source, subject)


def _count(lab: Lab, source: str, subject: str, role: FolderRole) -> int:
    return sum(1 for c in lab.copies(source, subject) if c.folder_role is role)


def _one_sent(lab: Lab, source: str, subject: str, where: str = "Sent") -> None:
    sent = _count(lab, source, subject, FolderRole.SENT)
    lab.check(sent == 1, f"expected exactly one copy in {where}, found {sent}")


def _pair(lab: Lab, t: Target, name: str) -> str:
    """The marked subject of a two-mailbox scenario; both mailboxes are cleaned up."""
    subject = lab.subject(f"{name} {t.tag}-{t.other_tag}")
    _own_mail(lab, t.source, subject)
    _own_mail(lab, t.other, subject)
    return subject


def _scratch(lab: Lab, t: Target) -> str:
    """The scratch folder id of the mailbox, or a skip when it has none."""
    folders = lab.env.probe.folders(t.source)
    scratch = scratch_folder(folders, lab.config.mail[t.source].scratch_folder)
    if scratch is None:
        raise ScenarioSkip("no scratch or archive folder is configured or found")
    return scratch.id


def _move(lab: Lab, t: Target, handles: list[str], folder: str, what: str, **more: Any) -> JSON:
    intent = {"kind": "mail.move", "source": t.source, "messages": handles, "to_folder": folder}
    return lab.confirmed(lab.write({**intent, **more}), what)


def _send_intent(lab: Lab, source: str, to: str, subject: str, text: str) -> JSON:
    return {
        "kind": "mail.send",
        "source": source,
        "to": [to],
        "subject": subject,
        "body": body(lab, text),
    }


@scenario(
    "mail.self_send",
    "mail",
    "Send to myself; it arrives; one copy in Sent",
    Scope.MAILBOX,
    needs=("mail.send", "mail.search"),
)
def self_send(lab: Lab, t: Target) -> None:
    subject, _, _ = _self_message(lab, t, "self_send")
    _one_sent(lab, t.source, subject)


@scenario(
    "mail.cross_send",
    "mail",
    "Send to another owned account; it arrives there",
    Scope.PAIR,
    needs=("mail.send",),
    needs_other=("mail.search",),
)
def cross_send(lab: Lab, t: Target) -> None:
    subject = _pair(lab, t, "cross_send")
    lab.send(t.source, lab.config.address_of(t.other), subject, body(lab, "cross-account send"))
    lab.arrival(t.other, subject)
    _one_sent(lab, t.source, subject, "the sender's Sent")


@scenario(
    "mail.reply_thread",
    "mail",
    "Reply in the thread; In-Reply-To arrives",
    Scope.PAIR,
    needs=("mail.send", "mail.search"),
    needs_other=("mail.send", "mail.search"),
)
def reply_thread(lab: Lab, t: Target) -> None:
    subject = _pair(lab, t, "reply_thread")
    lab.send(t.source, lab.config.address_of(t.other), subject, body(lab, "the original"))
    received = lab.arrival(t.other, subject)
    original_id = lab.message_id(received["handle"])
    reply_subject = f"Re: {subject}"
    lab.send(
        t.other,
        lab.config.address_of(t.source),
        reply_subject,
        body(lab, "the reply"),
        reply_to=received["handle"],
    )
    lab.arrival(t.source, reply_subject)
    inbox = [c for c in lab.copies(t.source, reply_subject) if c.folder_role is FolderRole.INBOX]
    lab.check(len(inbox) == 1, f"expected one reply in the inbox, found {len(inbox)}")
    lab.used_probe("In-Reply-To header of the arrived reply")
    seen = lab.env.probe.in_reply_to(t.source, inbox[0])
    lab.check(
        folded_message_id(seen) == folded_message_id(original_id),
        "the reply's In-Reply-To does not name the original message",
    )


@scenario(
    "mail.draft_flow",
    "mail",
    "Save a draft, save it again, then send it",
    Scope.MAILBOX,
    needs=("mail.send", "mail.draft", "mail.search"),
)
def draft_flow(lab: Lab, t: Target) -> None:
    address = lab.config.address_of(t.source)
    subject = lab.subject(f"draft_flow {t.tag}")
    _own_mail(lab, t.source, subject)
    saved = lab.confirmed(
        lab.write(
            {
                "kind": "mail.draft_save",
                "source": t.source,
                "to": [address],
                "subject": subject,
                "body": body(lab, "first version"),
            }
        ),
        "draft save",
    )
    handle = saved.get("result_ref")
    if handle is None:
        found = lab.hits(t.source, subject, folder="drafts")
        lab.check(len(found) == 1, f"expected one draft in Drafts, found {len(found)}")
        handle = found[0]["handle"]
    sha = lab.ok("open", handle=handle)["view"].get("raw_sha256")
    lab.check(sha is not None, "the draft shows no content hash to update against")
    lab.confirmed(
        lab.write(
            {
                "kind": "mail.draft_update",
                "source": t.source,
                "draft": handle,
                "expected_sha256": sha,
                "to": [address],
                "subject": subject,
                "body": body(lab, "second version"),
            }
        ),
        "draft re-save",
    )
    reopened = lab.call("open", handle=handle, part="body")
    lab.check(reopened.ok, "the draft handle did not survive the re-save")
    lab.check(
        "second version" in text_of(reopened.data),
        "the re-saved draft does not show the second version",
    )
    drafts = _count(lab, t.source, subject, FolderRole.DRAFTS)
    lab.check(drafts == 1, f"expected exactly one draft after re-saving, found {drafts}")
    lab.send(t.source, address, subject, body(lab, "second version"))
    lab.arrival(t.source, subject)
    _one_sent(lab, t.source, subject)


@scenario(
    "mail.attachment",
    "mail",
    "Attachment round trip keeps its hash",
    Scope.MAILBOX,
    needs=("mail.send", "mail.attachments", "mail.search"),
)
def attachment(lab: Lab, t: Target) -> None:
    address = lab.config.address_of(t.source)
    subject = lab.subject(f"attachment {t.tag}")
    _own_mail(lab, t.source, subject)
    text = f"{lab.marker} attachment\n" + "line of text, with an accent: café\n" * 40
    original = lab.ok(
        "keep",
        kind="artifact",
        owner=lab.action,
        text=text,
        reason="Live test attachment.",
        filename="live-test.txt",
        media_type="text/plain",
    )
    lab.defer("release the test attachment", lambda: _forget(lab, original["handle"]))
    lab.send(
        t.source,
        address,
        subject,
        body(lab, "with an attachment"),
        attachments=[
            {
                "artifact": original["handle"],
                "sha256": original["sha256"],
                "filename": "live-test.txt",
            }
        ],
    )
    arrived = lab.arrival(t.source, subject)
    listed = lab.ok("open", handle=arrived["handle"], part="attachments")["attachments"]
    lab.check(len(listed) == 1, f"expected one attachment on arrival, found {len(listed)}")
    copy = lab.ok(
        "keep",
        kind="artifact",
        owner=lab.action,
        from_handle=arrived["handle"],
        attachment=listed[0]["id"],
        reason="Live test arrival copy.",
    )
    lab.defer("release the arrived attachment", lambda: _forget(lab, copy["handle"]))
    lab.check(
        copy["sha256"] == original["sha256"],
        "the attachment's sha256 changed on the way",
    )


def _forget(lab: Lab, handle: str) -> None:
    lab.call("forget", handle=handle, owner=lab.action)


def _withdraw(lab: Lab, operation: str) -> None:
    """`forget` cancels a waiting operation and withdraws its proposal, so nothing can send it."""
    lab.ok("forget", handle=operation)
    state = lab.ok("open", handle=operation)["view"]["state"]
    lab.check(state == "cancelled", f"the refused operation is {state}, not cancelled")


def _unread(lab: Lab, handle: str) -> bool:
    return bool(lab.ok("open", handle=handle)["view"]["unread"])


@scenario(
    "mail.read_state",
    "mail",
    "Reading leaves mail unread; mark read and unread work",
    Scope.MAILBOX,
    needs=("mail.send", "mail.mark_read", "mail.search"),
)
def read_state(lab: Lab, t: Target) -> None:
    subject, _, hit = _self_message(lab, t, "read_state")
    handle = hit["handle"]
    before = _unread(lab, handle)
    lab.ok("open", handle=handle, part="body")
    lab.check(_unread(lab, handle) == before, "opening the message changed its read state (P-3)")
    # flip the state, then put it back: each step must really change what `open` shows
    for wanted_unread in (not before, before):
        lab.confirmed(
            lab.write(
                {
                    "kind": "mail.mark_read",
                    "source": t.source,
                    "messages": [handle],
                    "read": not wanted_unread,
                }
            ),
            f"mark {'unread' if wanted_unread else 'read'}",
        )
        lab.check(
            _unread(lab, handle) is wanted_unread,
            f"the message did not become {'unread' if wanted_unread else 'read'}",
        )
        flags = str(lab.hits(t.source, subject, folder="inbox")[0].get("summary", ""))
        lab.check(
            flags.startswith("unread") is wanted_unread,
            "find shows a read state that differs from open",
        )


@scenario(
    "mail.move_back",
    "mail",
    "File into the scratch folder and move back",
    Scope.MAILBOX,
    needs=("mail.send", "mail.move", "mail.search"),
)
def move_back(lab: Lab, t: Target) -> None:
    scratch = _scratch(lab, t)
    inbox = lab.folder_id(t.source, FolderRole.INBOX)
    subject, _, hit = _self_message(lab, t, "move_back")
    handle = hit["handle"]
    if any(c.folder_id == scratch for c in lab.copies(t.source, subject)):
        raise ScenarioSkip(
            "the scratch folder already holds the message (as All Mail does on Gmail); "
            "set live_test.mail.<source>.scratch_folder to a label that does not"
        )
    _move(lab, t, [handle], scratch, "move to the scratch folder")
    after = lab.copies(t.source, subject)
    lab.check(
        any(c.folder_id == scratch for c in after),
        "no copy is in the scratch folder after the move",
    )
    lab.check(
        not any(c.folder_id == inbox for c in after), "a copy is still in the inbox after the move"
    )
    lab.check(
        lab.call("open", handle=handle).ok, "the message handle did not survive the move (R4)"
    )
    _move(lab, t, [handle], inbox, "move back to the inbox")
    back = lab.copies(t.source, subject)
    lab.check(any(c.folder_id == inbox for c in back), "the message is not back in the inbox")


@scenario(
    "mail.batch_file",
    "mail",
    "File three messages in one write, marked read, and bring them back in one",
    Scope.MAILBOX,
    needs=("mail.send", "mail.move", "mail.search"),
)
def batch_file(lab: Lab, t: Target) -> None:
    scratch = _scratch(lab, t)
    inbox = lab.folder_id(t.source, FolderRole.INBOX)
    address = lab.config.address_of(t.source)
    common = lab.subject(f"batch_file {t.tag}")
    _own_mail(lab, t.source, common)
    subjects = [f"{common} {n}" for n in (1, 2, 3)]
    for subject in subjects:
        lab.send(t.source, address, subject, body(lab, "batch filing"))
    handles = [str(lab.arrival(t.source, subject)["handle"]) for subject in subjects]

    def where() -> list[set[str]]:
        copies = lab.copies(t.source, common)
        return [{c.folder_id for c in copies if c.subject.endswith(s[-2:])} for s in subjects]

    def batch(to_folder: str, mark_read: bool) -> JSON:
        return _move(lab, t, handles, to_folder, f"batch move to {to_folder}", mark_read=mark_read)

    view = batch(scratch, mark_read=True)
    lab.check(view.get("items_ok") == len(handles), f"only {view.get('items_ok')} of 3 moved")
    lab.check(all(scratch in f and inbox not in f for f in where()), "not all were filed")
    for handle in handles:
        lab.check(lab.call("open", handle=handle).ok, "a handle did not follow its message (R4)")
        lab.check(not _unread(lab, handle), "a filed message was not marked read")
    batch(inbox, mark_read=False)
    lab.check(all(inbox in f and scratch not in f for f in where()), "not all came back")


@scenario(
    "mail.trash_restore",
    "mail",
    "Trash a message and restore it",
    Scope.MAILBOX,
    needs=("mail.send", "mail.trash", "mail.move", "mail.search"),
)
def trash_restore(lab: Lab, t: Target) -> None:
    inbox = lab.folder_id(t.source, FolderRole.INBOX)
    subject, _, hit = _self_message(lab, t, "trash_restore")
    handle = hit["handle"]
    lab.trash(handle)
    after = lab.copies(t.source, subject)
    lab.check(any(c.folder_role is FolderRole.TRASH for c in after), "no copy is in the trash")
    lab.check(
        not any(c.folder_id == inbox for c in after), "a copy is still in the inbox after trashing"
    )
    operation = lab.prepare(
        {"kind": "mail.move", "source": t.source, "messages": [handle], "to_folder": inbox}
    )
    restored = lab.do(operation)
    if restored.code == "operation.uncertain":
        # Cleanup moves the marked copies again. Capture the evidence while it still exists.
        copies = lab.copies(t.source, subject)
        facts: list[str] = []
        for label, group in (
            ("Inbox", [c for c in copies if c.folder_id == inbox]),
            ("Trash", [c for c in copies if c.folder_role is FolderRole.TRASH]),
        ):
            facts.append(f"{label} {len(group)} copies ({sum(c.unread for c in group)} unread)")
        known_ids = {c.message_id for c in after if c.folder_role is FolderRole.TRASH}
        same = sum(c.message_id in known_ids for c in copies if c.folder_id == inbox)
        facts.append(f"{same} Inbox copies have the original Message-ID")
        # Diagnose the already-issued write before cleanup changes its evidence. Never repeat it.
        checked = lab.call("reconcile", operation=operation)
        state = str(lab.ok("open", handle=operation)["view"]["state"])
        facts.append(f"read-only recheck: {state if checked.ok else checked.code}")
        raise ScenarioFailure("restore uncertain before cleanup: " + ", ".join(facts))
    lab.confirmed(restored, "restore from the trash")
    back = lab.copies(t.source, subject)
    lab.check(any(c.folder_id == inbox for c in back), "the message is not back in the inbox")


@scenario(
    "mail.find_by_message_id",
    "mail",
    "find shows one hit per Message-ID across folders",
    Scope.MAILBOX,
    needs=("mail.send", "mail.search"),
)
def find_by_message_id(lab: Lab, t: Target) -> None:
    subject, _, _ = _self_message(lab, t, "find_by_message_id")
    copies = lab.copies(t.source, subject)
    ids = {folded_message_id(c.message_id) for c in copies}
    hits = lab.hits(t.source, subject)
    lab.check(
        len(hits) == len(ids),
        f"{len(ids)} Message-ID(s) in {len(copies)} copies, but find returned {len(hits)} hits",
    )
    for message_id in ids:
        folders = {c.folder_id for c in copies if folded_message_id(c.message_id) == message_id}
        if len(folders) > 1:
            lab.check(
                any("[" in text_of(hit) for hit in hits),
                "a message in several folders does not list them on its one hit",
            )


@scenario(
    "mail.repeat_do",
    "mail",
    "Repeating a do does not send twice",
    Scope.MAILBOX,
    needs=("mail.send", "mail.search"),
    alone=True,  # counts every dispatch of the run, so no other scenario may send meanwhile
)
def repeat_do(lab: Lab, t: Target) -> None:
    address = lab.config.address_of(t.source)
    subject = lab.subject(f"repeat_do {t.tag}")
    _own_mail(lab, t.source, subject)
    intent = _send_intent(lab, t.source, address, subject, "sent once")
    operation = lab.prepare(intent)
    lab.confirmed(lab.do(operation), "the first do")
    lab.arrival(t.source, subject)
    dispatched = lab.env.dispatches()
    again = lab.do(operation)
    lab.check(
        again.ok and again.data["operation"]["state"] == "confirmed",
        f"a second do did not report the finished send ({again.code or 'not confirmed'})",
    )
    proposed_again = lab.ok("propose", intent=intent, card=lab.card)
    lab.check(
        proposed_again["operation"] == operation,
        "proposing the same send again made a new operation",
    )
    lab.do(operation)
    if dispatched is not None:
        lab.check(lab.env.dispatches() == dispatched, "a repeated do reached the provider again")
    _one_sent(lab, t.source, subject, "Sent after repeating do")


@scenario(
    "mail.refuse_non_owned",
    "mail",
    "A send to a non-owned address is refused without sending",
    Scope.MAILBOX,
    needs=("mail.send", "mail.search"),
    alone=True,  # counts every dispatch of the run, so no other scenario may send meanwhile
)
def refuse_non_owned(lab: Lab, t: Target) -> None:
    subject = lab.subject(f"refuse_non_owned {t.tag}")
    _own_mail(lab, t.source, subject)
    dispatched = lab.env.dispatches()
    proposed = lab.ok(
        "propose",
        intent=_send_intent(lab, t.source, NOT_OWNED, subject, "must never be sent"),
        card=lab.card,
    )
    lab.check(proposed["ready"] is False, "a send to a stranger was ready without the owner")
    lab.check(
        proposed["requirement"] == "human_confirmation",
        f"a send to a stranger needs {proposed['requirement']}, not the owner's confirmation",
    )
    operation = str(proposed["operation"])
    lab.defer("withdraw the refused proposal", lambda: _withdraw(lab, operation))
    done = lab.call("do", operation=operation)
    lab.check(not done.ok, "do accepted a send that no grant covers")
    state = lab.ok("open", handle=proposed["operation"])["view"]["state"]
    lab.check(
        state in {"proposed", "failed_clean", "cancelled"},
        f"the refused operation is {state}, not stopped before dispatch",
    )
    lab.check(
        not lab.copies(t.source, subject), "a copy of the refused message exists in the mailbox"
    )
    if dispatched is not None:
        lab.check(lab.env.dispatches() == dispatched, "the refused send reached the provider")


@scenario(
    "mail.fault_lost_response",
    "fault",
    "A lost response ends settled or uncertain, with exactly one Sent copy",
    Scope.MAILBOX,
    needs=("mail.send", "mail.search"),
    alone=True,  # the fault hook fires on the next marked send, whichever scenario makes it
)
def fault_lost_response(lab: Lab, t: Target) -> None:
    unavailable = lab.env.faults.why_unavailable()
    if unavailable is not None:
        raise ScenarioSkip(f"cannot arm a lost response: {unavailable}")
    address = lab.config.address_of(t.source)
    subject = lab.subject(f"fault_lost_response {t.tag}")
    _own_mail(lab, t.source, subject)
    lab.defer("disarm the fault", lab.env.faults.disarm)
    operation = lab.prepare(
        _send_intent(lab, t.source, address, subject, "the response to this send is dropped")
    )
    lab.env.faults.arm_lost_response(lab.run_id)
    first = lab.do(operation)
    if not lab.env.faults.fired():
        raise ScenarioSkip(
            "the lost-response hook did not fire; restart Thunderbird so the native host "
            "reloads, then run again"
        )
    state = _state_after(lab, operation)
    lab.check(
        state in {"confirmed", "uncertain", "reconciling"},
        f"after a lost response the operation is {state}",
    )
    _one_sent(lab, t.source, subject, "Sent after a lost response")
    lab.do(operation)
    after = _count(lab, t.source, subject, FolderRole.SENT)
    lab.check(after == 1, f"a repeated do after the fault left {after} copies in Sent")
    if state != "confirmed":
        lab.accepted_uncertain.add(operation)
        lab.check(first.code == "operation.uncertain", "the lost send was not reported uncertain")


def _state_after(lab: Lab, operation: str) -> str:
    """Let the harness settle the write: read the state, reconcile, wait, a few times."""
    for _ in range(6):
        state = str(lab.ok("open", handle=operation)["view"]["state"])
        if state == "confirmed":
            return state
        lab.call("reconcile", operation=operation)
        lab.env.sleep(5)
    return str(lab.ok("open", handle=operation)["view"]["state"])


@scenario(
    "mail.watermark_suggestion",
    "mail",
    "Arriving mail is picked up and the reply gets a suggested card",
    Scope.PAIR,
    needs=("mail.send", "mail.search"),
    needs_other=("mail.send", "mail.search"),
    alone=True,  # reads the harness's whole new-mail batch, which other scenarios change
)
def watermark_suggestion(lab: Lab, t: Target) -> None:
    subject = _pair(lab, t, "watermark")
    lab.send(t.source, lab.config.address_of(t.other), subject, body(lab, "the first mail"))
    received = lab.arrival(t.other, subject)
    reply_subject = f"Re: {subject}"
    lab.send(
        t.other,
        lab.config.address_of(t.source),
        reply_subject,
        body(lab, "the reply"),
        reply_to=received["handle"],
    )
    lab.arrival(t.source, reply_subject)
    lab.tick()
    hit = lab.hits(t.source, reply_subject, folder="inbox")[0]
    if hit.get("suggested_card") != lab.card:
        raise ScenarioFailure(
            "the reply got no suggested card: either the tick did not pick the mail up, or the "
            "sender does not match exactly one open card (another open card that also sent to "
            "this address hides the suggestion)"
        )


@scenario(
    "mail.outbox_queued",
    "mail",
    "A send waits in the Outbox and is not repeated (manual)",
    Scope.GLOBAL,
    manual=(
        "Close Thunderbird's network (File > Work Offline), or stop the mail server.",
        "Run: chamberlain live-test run --only mail.self_send --keep, then read the operation.",
        "Expect the operation to stay uncertain and `reconcile` to report it queued.",
        "Go online again; the message sends once. Check that Sent holds exactly one copy.",
        "Then run: chamberlain live-test sweep",
    ),
)
def outbox_queued(lab: Lab, t: Target) -> None:
    raise ScenarioSkip("manual scenario: it cannot be forced safely; the steps are in the report")


OWNER_CONFIRM_SECONDS = 900
"""How long `mail.approved_send` waits for the owner to confirm the held reply in the viewer."""


def _owner_action(lab: Lab, title: str) -> str:
    """An action on the test card waiting for the owner; finished in cleanup if still open."""
    card = lab.ok(
        "card",
        op="add_action",
        card=lab.card,
        action_fields={"title": title, "for_owner": True},
    )["card"]
    action = next(str(a["handle"]) for a in card["actions"] if a["title"] == title)
    lab.defer(
        f"finish the test action {action}",
        lambda: lab.call("card", op="finish_action", action=action, outcome="Live test cleanup."),
    )
    return action


def _dispatched(lab: Lab, source: str, content_sha256: str) -> int:
    """Operations for this exact content that were handed to the mail client (read only)."""
    return sum(
        1
        for op in repo.operations_by_hash(lab.app.context.db, source, content_sha256)
        if op.dispatched_at is not None
    )


def _composers(lab: Lab, source: str) -> tuple[int, int] | None:
    lab.used_probe("composer windows open (the harness's and the owner's)")
    return lab.env.probe.composers(source)


@scenario(
    "mail.approved_send",
    "mail",
    "A reply held for the owner goes once, headless, when they confirm it",
    Scope.PAIR,
    needs=("mail.send",),
    needs_other=("mail.search",),
    alone=True,  # waits for the owner's click in the viewer
)
def approved_send(lab: Lab, t: Target) -> None:
    subject = _pair(lab, t, "approved_send")
    action = _owner_action(lab, f"Approve the reply to {t.other_tag}: live test {lab.run_id}")
    before = _composers(lab, t.source)
    dispatches = lab.env.dispatches()
    intent = _send_intent(lab, t.source, lab.config.address_of(t.other), subject, "approved send")
    proposed = lab.call("propose", intent=intent, card=lab.card, action=action)
    if not proposed.ok:
        raise ScenarioFailure(f"propose of the held reply failed: {proposed.code}")
    held = proposed.data
    operation, grant = str(held["operation"]), str(held["grant"])
    lab.check(not held["ready"], "a reply held for the owner was ready without their click")
    lab.check(
        held["requirement"] == "human_confirmation",
        f"the held reply needs {held['requirement']}, not the owner's confirmation",
    )
    lab.check(not lab.call("do", operation=operation).ok, "do sent the reply before the owner")
    if lab.env.owner_confirms is not None:
        lab.env.owner_confirms(grant)
    else:
        lab.env.notify(
            f"Live test: open the viewer and confirm grant {grant} (the held reply "
            f"'{subject}'). Waiting up to {OWNER_CONFIRM_SECONDS // 60} minutes."
        )
    lab.wait_until(
        "the owner's confirmation and the approved send",
        lambda: (
            True
            if str(lab.ok("open", handle=operation)["view"]["state"])
            not in ("proposed", "authorised")
            else None
        ),
        seconds=OWNER_CONFIRM_SECONDS,
    )
    state = _state_after(lab, operation)
    lab.check(state == "confirmed", f"the approved send ended {state}, not confirmed")
    content = str(held["content_sha256"])
    sent = _dispatched(lab, t.source, content)
    lab.check(sent == 1, f"expected one submission of the approved text, found {sent}")
    after_dispatches = lab.env.dispatches()
    if dispatches is not None and after_dispatches is not None:
        lab.check(after_dispatches - dispatches == 1, "the client received more than one write")
    lab.arrival(t.other, subject)
    received = _count(lab, t.other, subject, FolderRole.INBOX)
    lab.check(received == 1, f"expected one copy at the recipient, found {received}")
    _one_sent(lab, t.source, subject, "the sender's Sent")
    finished = lab.app.context.db.execute(
        "SELECT state, outcome FROM actions WHERE handle = ?", (action,)
    ).fetchone()
    lab.check(
        finished is not None
        and finished["state"] == "done"
        and str(finished["outcome"]).startswith("Sent as approved at "),
        "the owner's action was not finished with the send's outcome",
    )
    after = _composers(lab, t.source)
    if before is None or after is None:
        raise ScenarioFailure(
            "the mail client cannot count composer windows (update the add-on to 3.1.0 or later)"
        )
    lab.check(after[0] == 0, f"the harness left {after[0]} composer window(s) open")
    lab.check(after[1] == before[1], "the number of the owner's own composer windows changed")
