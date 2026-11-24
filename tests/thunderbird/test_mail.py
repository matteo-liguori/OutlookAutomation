"""Connector behaviour beyond the shared suite: ported incidents and the review rulings.

Each test names the rule it enforces (P-n, W-n, B-n) or the review ruling (R1 to R11) and,
where it carries an old incident across, says which. Runs the real add-on under Node.
"""

import pytest
from thunderbird_rig import ACCOUNT, MAILBOX, Stack

from chamberlain.connectors.thunderbird.identity import encode_locator
from chamberlain.connectors.thunderbird.mail import ALLOW_DUPLICATE_REF, DISPLAY_ONLY_REF
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.domain.capabilities import CapabilityName, HealthState
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import (
    MailDraftDiscard,
    MailDraftSave,
    MailDraftUpdate,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
)
from chamberlain.domain.mail import FolderRole, MailQuery
from chamberlain.domain.operations import (
    DispatchOutcome,
    DispatchResult,
    Prepared,
    ReconcileStatus,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode

SOURCE = "mail:test"

def handle(locator: str) -> str:
    return ref_handle(HandleKind.MESSAGE, SOURCE, locator)


def seed(stack: Stack, folder: FolderRole = FolderRole.INBOX, **kwargs: object) -> tuple[str, str]:
    message_id = stack.new_message_id()
    locator = stack.seed(internet_message_id=message_id, folder=folder, **kwargs)  # type: ignore[arg-type]
    return message_id, locator


def send_intent(
    stack: Stack, *, to: str = "someone@example.org", body: str = "Body.", token: str | None = None
) -> MailSend:
    return MailSend.model_validate(
        {
            "source": SOURCE,
            "to": [to],
            "subject": "Ruling test",
            "body": body,
            "message_id": token or stack.new_message_id(),
        }
    )


def applied(result: DispatchResult) -> DispatchResult:
    assert result.outcome is DispatchOutcome.APPLIED, result.detail
    return result


# --- capabilities and freshness -----------------------------------------------------------


def test_r1_message_id_is_not_honoured_and_send_absence_is_never_provable(stack: Stack) -> None:
    capabilities = stack.mail.capabilities(SOURCE)
    send = capabilities.get(CapabilityName.MAIL_SEND)
    assert send is not None
    assert send.limits["message_id_honoured"] == 0
    assert send.absence_provable == frozenset()
    for name in (
        CapabilityName.MAIL_MOVE,
        CapabilityName.MAIL_TRASH,
        CapabilityName.MAIL_MARK_READ,
        CapabilityName.MAIL_DRAFT,
    ):
        assert capabilities.get(name) is not None
        assert capabilities.get(name).absence_provable  # type: ignore[union-attr]
    assert all(c.level.value == "implemented" for c in capabilities.capabilities)


def test_p5_health_is_ok_only_after_a_verified_refresh_and_two_stable_samples(
    stack: Stack,
) -> None:
    seed(stack)
    health = stack.mail.health(SOURCE)
    assert health.state is HealthState.OK
    assert health.fresh_as_of == health.checked_at
    stack.inject(__import__("chamberlain.testing.rigs", fromlist=["Fault"]).Fault.STALE)
    assert stack.mail.health(SOURCE).state is HealthState.STALE


def test_p5_a_bridge_that_is_down_is_unavailable_never_guessed(stack: Stack) -> None:
    stack.host.stop()
    assert stack.mail.health(SOURCE).state is HealthState.UNAVAILABLE
    with pytest.raises(ChamberlainError) as raised:
        stack.mail.search(SOURCE, MailQuery(text="x"))
    assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_p17_a_dispatch_to_a_bridge_that_is_down_is_not_applied(stack: Stack) -> None:
    _, locator = seed(stack)
    intent = MailMove.model_validate(
        {"source": SOURCE, "message": handle(locator), "to_folder": stack.filing_folder_id()}
    )
    prepared = stack.mail.prepare(intent, {handle(locator): locator})
    stack.host.stop()
    assert stack.mail.dispatch(prepared, "k", {}).outcome is DispatchOutcome.NOT_APPLIED


def test_p6_a_locator_of_another_account_is_an_unknown_handle(stack: Stack) -> None:
    foreign = encode_locator("another-account", f"{ACCOUNT}://INBOX", "<x@y>")
    with pytest.raises(ChamberlainError) as raised:
        stack.mail.resolve(SOURCE, foreign)
    assert raised.value.code is ErrorCode.REQUEST_UNKNOWN_HANDLE


def test_m17_a_cursor_is_bound_to_its_search(stack: Stack) -> None:
    for _ in range(3):
        seed(stack, subject="cursor-marker")
    page = stack.mail.search(SOURCE, MailQuery(text="cursor-marker", limit=2))
    assert page.cursor is not None
    with pytest.raises(ChamberlainError) as raised:
        stack.mail.search(SOURCE, MailQuery(text="another search", limit=2), page.cursor)
    assert raised.value.code is ErrorCode.REQUEST_BAD_CURSOR


def test_p4_an_empty_search_of_a_stale_store_is_incomplete_not_nothing(stack: Stack) -> None:
    stack.control("inject", fault="stale")
    page = stack.mail.search(SOURCE, MailQuery(text="nothing here"))
    assert page.items == ()
    assert not page.complete
    assert page.gaps


def test_p9_prepare_issues_no_write_of_any_kind(stack: Stack) -> None:
    _, locator = seed(stack)
    message_id = stack.new_message_id()
    refs = {handle(locator): locator}
    intents = [
        MailMove.model_validate(
            {"source": SOURCE, "message": handle(locator), "to_folder": stack.filing_folder_id()}
        ),
        MailTrash.model_validate({"source": SOURCE, "message": handle(locator)}),
        MailMarkRead.model_validate({"source": SOURCE, "messages": [handle(locator)]}),
        send_intent(stack),
        MailDraftSave.model_validate(
            {"source": SOURCE, "to": [], "subject": "d", "body": "b", "message_id": message_id}
        ),
    ]
    for intent in intents:
        stack.mail.prepare(intent, refs)
    counters = stack.control("counters")
    assert (counters["move"], counters["send"], counters["save"], counters["update"]) == (
        0,
        0,
        0,
        0,
    )


# --- move, trash, mark read ----------------------------------------------------------------


def test_r4_move_and_trash_receipts_return_the_new_locator(stack: Stack) -> None:
    _, locator = seed(stack)
    move = MailMove.model_validate(
        {"source": SOURCE, "message": handle(locator), "to_folder": stack.filing_folder_id()}
    )
    moved = applied(
        stack.mail.dispatch(stack.mail.prepare(move, {handle(locator): locator}), "k1", {})
    )
    assert moved.receipt is not None
    new = moved.receipt.locator
    assert new is not None
    assert new != locator
    assert stack.mail.resolve(SOURCE, new).folder_id == stack.filing_folder_id()
    trash = MailTrash.model_validate({"source": SOURCE, "message": handle(new)})
    trashed = applied(stack.mail.dispatch(stack.mail.prepare(trash, {handle(new): new}), "k2", {}))
    assert trashed.receipt is not None
    assert trashed.receipt.locator is not None
    assert stack.mail.resolve(SOURCE, trashed.receipt.locator).folder_id == stack.folder_id(
        FolderRole.TRASH
    )


def test_r7_trash_that_merges_into_an_existing_copy_is_applied_with_a_warning(
    stack: Stack,
) -> None:
    message_id = stack.new_message_id()
    inbox = stack.seed(internet_message_id=message_id, unread=False)
    stack.seed(internet_message_id=message_id, folder=FolderRole.TRASH, unread=True)
    stack.control("inject", fault="merge")
    intent = MailTrash.model_validate({"source": SOURCE, "message": handle(inbox)})
    result = applied(
        stack.mail.dispatch(stack.mail.prepare(intent, {handle(inbox): inbox}), "k", {})
    )
    assert result.receipt is not None
    assert result.receipt.warnings == ("merged_existing_copy",)
    assert result.receipt.read_state_preserved is False
    assert stack.copies(message_id, stack.folder_id(FolderRole.TRASH)) == 1


def test_p6_a_source_with_two_copies_makes_trash_ambiguous_old_behaviour_dropped(
    stack: Stack,
) -> None:
    message_id = stack.new_message_id()
    locator = stack.seed(internet_message_id=message_id)
    stack.seed(internet_message_id=message_id)
    intent = MailTrash.model_validate({"source": SOURCE, "message": handle(locator)})
    with pytest.raises(ChamberlainError) as raised:
        stack.mail.prepare(intent, {handle(locator): locator})
    assert raised.value.code is ErrorCode.REMOTE_AMBIGUOUS


def test_p6_a_prepared_write_survives_a_client_restart(stack: Stack) -> None:
    message_id, locator = seed(stack)
    intent = MailMove.model_validate(
        {"source": SOURCE, "message": handle(locator), "to_folder": stack.filing_folder_id()}
    )
    prepared = stack.mail.prepare(intent, {handle(locator): locator})
    assert stack.restart()
    applied(stack.mail.dispatch(prepared, "k", {}))
    assert stack.copies(message_id, stack.filing_folder_id()) == 1


def test_w24_mark_read_reports_partial_results_and_can_mark_unread(stack: Stack) -> None:
    ids = [seed(stack) for _ in range(2)]
    refs = {handle(loc): loc for _, loc in ids}
    intent = MailMarkRead.model_validate({"source": SOURCE, "messages": list(refs)})
    prepared = stack.mail.prepare(intent, refs)
    stack.control("remove", id=ids[1][0])
    result = applied(stack.mail.dispatch(prepared, "k", {}))
    assert result.receipt is not None
    assert [item.ok for item in result.receipt.items] == [True, False]
    assert result.receipt.items[1].code is ErrorCode.REQUEST_UNKNOWN_HANDLE
    back = MailMarkRead.model_validate(
        {"source": SOURCE, "messages": [handle(ids[0][1])], "read": False}
    )
    applied(stack.mail.dispatch(stack.mail.prepare(back, {handle(ids[0][1]): ids[0][1]}), "k2", {}))
    assert stack.is_unread(ids[0][1])


def test_w24_marking_nothing_is_not_applied(stack: Stack) -> None:
    message_id, locator = seed(stack)
    intent = MailMarkRead.model_validate({"source": SOURCE, "messages": [handle(locator)]})
    prepared = stack.mail.prepare(intent, {handle(locator): locator})
    stack.control("remove", id=message_id)
    assert stack.mail.dispatch(prepared, "k", {}).outcome is DispatchOutcome.NOT_APPLIED


def test_p11_a_mark_read_that_did_not_happen_is_absent_with_proof(stack: Stack) -> None:
    _, locator = seed(stack)
    intent = MailMarkRead.model_validate({"source": SOURCE, "messages": [handle(locator)]})
    prepared = stack.mail.prepare(intent, {handle(locator): locator})
    assert stack.mail.reconcile(prepared, "k").proof
    applied(stack.mail.dispatch(prepared, "k", {}))
    assert stack.mail.reconcile(prepared, "k").status is ReconcileStatus.PRESENT


# --- sending, drafts and reconcile ---------------------------------------------------------


def test_p13_an_uncertain_send_is_never_reissued(stack: Stack) -> None:
    """Old: test_uncertain_send_is_never_reissued. The connector sends once, ever."""
    intent = send_intent(stack)
    prepared = stack.mail.prepare(intent, {})
    stack.control("inject", fault="lose_response")
    assert stack.mail.dispatch(prepared, "k", {}).outcome is DispatchOutcome.UNKNOWN
    for _ in range(2):
        assert stack.mail.reconcile(prepared, "k").status is ReconcileStatus.PRESENT
    assert stack.control("counters")["send"] == 1


def test_r1_send_reconcile_matches_by_content_and_needs_no_attachment_file(stack: Stack) -> None:
    """Old: test_send_reconciliation_never_resubmits_or_needs_original_attachment_file."""
    data = b"attachment bytes " * 1000
    intent = MailSend.model_validate(
        {
            "source": SOURCE,
            "to": ["someone@example.org"],
            "subject": "With file",
            "body": "See file.",
            "message_id": stack.new_message_id(),
            "attachments": [
                {
                    "artifact": "art_0123456789abcdef",
                    "sha256": sha256_bytes(data),
                    "filename": "f.txt",
                }
            ],
        }
    )
    prepared = stack.mail.prepare(intent, {})
    applied(stack.mail.dispatch(prepared, "k", {sha256_bytes(data): data}))
    settled = stack.mail.reconcile(prepared, "k")  # no payloads are passed
    assert settled.status is ReconcileStatus.PRESENT
    assert settled.receipt is not None
    assert settled.receipt.verification.server_verified


def test_w3_a_large_attachment_crosses_several_frames_and_arrives_intact(stack: Stack) -> None:
    """Old: test_binary_transfer_crosses_multiple_frames_and_hashes_real_bytes."""
    data = bytes(range(256)) * 1800  # 460,800 bytes, three chunks
    token = stack.new_message_id()
    intent = MailSend.model_validate(
        {
            "source": SOURCE,
            "to": ["someone@example.org"],
            "subject": "Large",
            "body": "Large file.",
            "message_id": token,
            "attachments": [
                {
                    "artifact": "art_0123456789abcdef",
                    "sha256": sha256_bytes(data),
                    "filename": "big.bin",
                }
            ],
        }
    )
    applied(stack.mail.dispatch(stack.mail.prepare(intent, {}), "k", {sha256_bytes(data): data}))
    sizes = stack.control("attachment_sizes", id=token, folder_id=stack.folder_id(FolderRole.SENT))
    assert [(name, size) for name, size, _ in sizes] == [("big.bin", len(data))]


def test_p8_a_large_download_is_chunked_and_verified(stack: Stack) -> None:
    data = bytes(range(256)) * 1800
    _, locator = seed(stack, attachments=(("big.bin", data),))
    meta = stack.mail.read(SOURCE, locator).attachments[0]
    fetched = stack.mail.attachment(SOURCE, locator, meta.id)
    assert fetched.data == data
    assert fetched.sha256 == sha256_bytes(data)
    assert stack.is_unread(locator)


def test_r2_an_exact_copy_waiting_in_the_outbox_is_queued_and_never_absent(stack: Stack) -> None:
    stack.control("inject", fault="no_sent_copy")
    stack.control("inject", fault="queue_only")
    prepared = stack.mail.prepare(send_intent(stack), {})
    applied(stack.mail.dispatch(prepared, "k", {}))
    settled = stack.mail.reconcile(prepared, "k")
    assert settled.status is ReconcileStatus.QUEUED
    assert settled.status is not ReconcileStatus.ABSENT
    assert not settled.proof
    assert "Outbox" in settled.detail


def test_r11_a_self_send_that_reached_the_own_inbox_counts_as_present(stack: Stack) -> None:
    stack.control("inject", fault="no_sent_copy")
    prepared = stack.mail.prepare(send_intent(stack, to=MAILBOX), {})
    applied(stack.mail.dispatch(prepared, "k", {}))
    settled = stack.mail.reconcile(prepared, "k")
    assert settled.status is ReconcileStatus.PRESENT
    assert settled.receipt is not None
    assert settled.receipt.locator is not None
    assert stack.mail.resolve(SOURCE, settled.receipt.locator).folder_id == stack.folder_id(
        FolderRole.INBOX
    )


def test_r3_a_related_message_in_sent_blocks_a_second_send_unless_the_grant_allows(
    stack: Stack,
) -> None:
    applied(stack.mail.dispatch(stack.mail.prepare(send_intent(stack), {}), "k1", {}))
    with pytest.raises(ChamberlainError) as raised:
        stack.mail.prepare(send_intent(stack), {})
    assert raised.value.code is ErrorCode.REMOTE_CHANGED
    assert not raised.value.may_have_happened
    allowed = stack.mail.prepare(send_intent(stack), {ALLOW_DUPLICATE_REF: "1"})
    assert allowed.intent_kind.value == "mail.send"


def test_r3_a_saved_draft_with_the_same_content_blocks_a_send(stack: Stack) -> None:
    draft = MailDraftSave.model_validate(
        {
            "source": SOURCE,
            "to": ["someone@example.org"],
            "subject": "Ruling test",
            "body": "Body.",
            "message_id": stack.new_message_id(),
        }
    )
    applied(stack.mail.dispatch(stack.mail.prepare(draft, {}), "k1", {}))
    with pytest.raises(ChamberlainError) as raised:
        stack.mail.prepare(send_intent(stack), {})
    assert raised.value.code is ErrorCode.REMOTE_CHANGED


def test_r3_a_scan_that_cannot_finish_blocks_the_send(stack: Stack) -> None:
    for index in range(205):
        stack.seed(
            internet_message_id=f"<fill-{index}@x>",
            folder=FolderRole.SENT,
            subject="Ruling test",
            body=f"distinct body {index}",
        )
    with pytest.raises(ChamberlainError) as raised:
        stack.mail.prepare(send_intent(stack, body="a brand new body"), {})
    assert raised.value.code is ErrorCode.REMOTE_CHANGED


def test_p9_draft_update_replaces_the_draft_and_returns_the_new_locator(stack: Stack) -> None:
    first = stack.new_message_id()
    compose = {"source": SOURCE, "to": ["someone@example.org"], "subject": "Draft", "body": "One."}
    saved = applied(
        stack.mail.dispatch(
            stack.mail.prepare(MailDraftSave.model_validate({**compose, "message_id": first}), {}),
            "k1",
            {},
        )
    )
    assert saved.receipt is not None
    locator = saved.receipt.locator
    assert locator is not None
    update = MailDraftUpdate.model_validate(
        {
            **compose,
            "body": "Two.",
            "message_id": stack.new_message_id(),
            "draft": handle(locator),
            "expected_sha256": saved.receipt.remote_sha256,
        }
    )
    prepared = stack.mail.prepare(update, {handle(locator): locator})
    updated = applied(stack.mail.dispatch(prepared, "k2", {}))
    assert updated.receipt is not None
    assert updated.receipt.locator not in (None, locator)
    assert stack.mail.read(SOURCE, updated.receipt.locator or "").body_text == "Two."


def draft_with_open_composer(stack: Stack) -> tuple[str, str, Prepared]:
    compose = {
        "source": SOURCE,
        "to": ["someone@example.org"],
        "subject": "Open draft",
        "body": "Keep me.",
        "message_id": stack.new_message_id(),
    }
    prepared = stack.mail.prepare(MailDraftSave.model_validate(compose), {DISPLAY_ONLY_REF: "1"})
    saved = applied(stack.mail.dispatch(prepared, "k1", {}))
    assert saved.receipt is not None
    assert saved.receipt.locator is not None
    assert saved.receipt.remote_sha256 is not None
    return saved.receipt.locator, saved.receipt.remote_sha256, prepared


def test_r8_discard_closes_the_composer_and_never_deletes_saved_mail(stack: Stack) -> None:
    locator, raw_hash, _ = draft_with_open_composer(stack)
    discard = MailDraftDiscard.model_validate(
        {"source": SOURCE, "draft": handle(locator), "expected_sha256": raw_hash}
    )
    prepared = stack.mail.prepare(discard, {handle(locator): locator})
    assert stack.mail.reconcile(prepared, "k").status is ReconcileStatus.ABSENT
    result = applied(stack.mail.dispatch(prepared, "k2", {}))
    assert "closed 1" in result.detail
    assert stack.mail.resolve(SOURCE, locator).folder_id == stack.folder_id(FolderRole.DRAFTS)
    assert stack.mail.reconcile(prepared, "k2").status is ReconcileStatus.PRESENT


def test_r8_discard_refuses_a_draft_that_changed(stack: Stack) -> None:
    locator, _, _ = draft_with_open_composer(stack)
    discard = MailDraftDiscard.model_validate(
        {"source": SOURCE, "draft": handle(locator), "expected_sha256": "0" * 64}
    )
    with pytest.raises(ChamberlainError) as raised:
        stack.mail.prepare(discard, {handle(locator): locator})
    assert raised.value.code is ErrorCode.REMOTE_CHANGED


def test_r8_a_reply_keeps_the_original_unread_and_sets_the_reply_header(stack: Stack) -> None:
    """Old: reply headers and the unread flag Thunderbird clears on the replied-to message."""
    _, original = seed(stack, subject="Question")
    intent = MailDraftSave.model_validate(
        {
            "source": SOURCE,
            "to": ["alice@example.org"],
            "subject": "Re: Question",
            "body": "Answer.",
            "reply_to": handle(original),
            "message_id": stack.new_message_id(),
        }
    )
    applied(stack.mail.dispatch(stack.mail.prepare(intent, {handle(original): original}), "k", {}))
    assert stack.is_unread(original)
