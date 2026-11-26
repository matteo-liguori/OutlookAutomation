"""The MailPort contract suite.

Contract: docs/contracts/08-ports.md. To plug a connector in:

    class TestThunderbirdMail(MailPortContract):
        @pytest.fixture
        def rig(self) -> MailRig:
            return ThunderbirdRig(...)

Each test names the rule it enforces. Tests that need a fault the rig cannot
inject are skipped.
"""

import pytest

from chamberlain.domain.canonical import sha256_bytes
from chamberlain.domain.capabilities import CapabilityName
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import (
    AttachmentRef,
    IntentKind,
    MailDraftSave,
    MailDraftUpdate,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
)
from chamberlain.domain.mail import FolderRole, MailQuery
from chamberlain.domain.operations import DispatchOutcome, Prepared, ReconcileStatus
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from support.rigs import Fault, MailRig

LATE = 600.0
"""Seconds after which a slow write has certainly landed."""


def _handle(rig: MailRig, locator: str) -> str:
    return ref_handle(HandleKind.MESSAGE, rig.source, locator)


def _need(rig: MailRig, fault: Fault) -> None:
    if not rig.inject(fault):
        pytest.skip(f"this rig cannot inject {fault.value}")


class MailPortContract:
    """Subclass and provide a `rig` fixture returning a fresh `MailRig`."""

    # --- helpers --------------------------------------------------------------

    def _prepare_move(self, rig: MailRig, locator: str, *, mark_read: bool = False) -> Prepared:
        handle = _handle(rig, locator)
        intent = MailMove.model_validate(
            {
                "source": rig.source,
                "messages": [handle],
                "to_folder": rig.filing_folder_id(),
                "mark_read": mark_read,
            }
        )
        return rig.port.prepare(intent, {handle: locator})

    def _prepare_send(
        self, rig: MailRig, message_id: str, attachments: tuple[AttachmentRef, ...] = ()
    ) -> Prepared:
        intent = MailSend.model_validate(
            {
                "source": rig.source,
                "to": ["owner@example.org"],
                "subject": "Contract send",
                "body": "Sent by the contract suite.",
                "message_id": message_id,
                "attachments": attachments,
            }
        )
        return rig.port.prepare(intent, {})

    # --- capabilities and reading ---------------------------------------------

    def test_p1_capabilities_are_declared_for_the_source(self, rig: MailRig) -> None:
        declared = rig.port.capabilities(rig.source)
        assert declared.source == rig.source
        assert declared.get(CapabilityName.MAIL_READ) is not None
        assert all(c.name.value.startswith("mail.") for c in declared.capabilities)

    def test_p3_no_read_changes_the_unread_flag(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id, attachments=(("a.txt", b"abc"),))
        rig.port.search(rig.source, MailQuery(internet_message_id=message_id))
        message = rig.port.read(rig.source, locator)
        rig.port.resolve(rig.source, locator)
        rig.port.attachment(rig.source, locator, message.attachments[0].id)
        rig.port.folders(rig.source)
        assert rig.is_unread(locator)

    def test_p4_pages_cover_everything_once(self, rig: MailRig) -> None:
        ids = [rig.new_message_id() for _ in range(5)]
        for message_id in ids:
            rig.seed(internet_message_id=message_id, subject="paging-marker")
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            page = rig.port.search(rig.source, MailQuery(text="paging-marker", limit=2), cursor)
            seen.extend(item.internet_message_id for item in page.items)
            cursor = page.cursor
            if cursor is None:
                assert page.complete
                break
            assert not page.complete
        assert sorted(seen) == sorted(ids)

    def test_p4_a_stale_store_is_reported_incomplete(self, rig: MailRig) -> None:
        _need(rig, Fault.STALE)
        page = rig.port.search(rig.source, MailQuery(text="nothing-matches-this"))
        assert not page.items
        assert not page.complete
        assert page.gaps

    def test_p6_locator_survives_a_client_restart(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        if not rig.restart():
            pytest.skip("this rig cannot restart the client")
        assert rig.port.resolve(rig.source, locator).internet_message_id == message_id

    def test_p6_an_unknown_locator_is_an_error_not_a_guess(self, rig: MailRig) -> None:
        with pytest.raises(ChamberlainError) as raised:
            rig.port.resolve(rig.source, "no-such-folder/<no-such-message@example.org>")
        assert raised.value.code is ErrorCode.REQUEST_UNKNOWN_HANDLE

    def test_p7_physical_copies_stay_distinct(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        first = rig.seed(internet_message_id=message_id, folder=FolderRole.INBOX)
        second = rig.seed(internet_message_id=message_id, folder=FolderRole.ARCHIVE)
        page = rig.port.search(rig.source, MailQuery(internet_message_id=message_id))
        assert first != second
        assert sorted(item.locator for item in page.items) == sorted([first, second])

    def test_p8_attachment_bytes_come_with_their_hash(self, rig: MailRig) -> None:
        # Large enough to cross several frames on a bridge with chunked attachments.
        payload = bytes(range(256)) * 1800
        locator = rig.seed(
            internet_message_id=rig.new_message_id(), attachments=(("blob.bin", payload),)
        )
        meta = rig.port.read(rig.source, locator).attachments[0]
        fetched = rig.port.attachment(rig.source, locator, meta.id)
        assert fetched.data == payload
        assert fetched.sha256 == sha256_bytes(payload)
        assert rig.is_unread(locator)

    # --- move and trash -------------------------------------------------------

    def test_p9_prepare_changes_nothing(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        self._prepare_move(rig, locator, mark_read=True)
        assert rig.copies(message_id, rig.folder_id(FolderRole.INBOX)) == 1
        assert rig.copies(message_id, rig.filing_folder_id()) == 0
        assert rig.is_unread(locator)

    def test_p14_move_leaves_exactly_one_copy_and_the_flag_alone(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        result = rig.port.dispatch(self._prepare_move(rig, locator), "k-move", {})
        assert result.outcome is DispatchOutcome.APPLIED
        assert result.receipt is not None
        moved_to = result.receipt.items[0].moved_to
        assert moved_to is not None
        assert moved_to != locator
        assert rig.copies(message_id, rig.folder_id(FolderRole.INBOX)) == 0
        assert rig.copies(message_id, rig.filing_folder_id()) == 1
        assert rig.port.resolve(rig.source, moved_to).folder_id == rig.filing_folder_id()
        assert rig.is_unread(moved_to)

    def test_w23_move_marks_read_only_when_told_to(self, rig: MailRig) -> None:
        locator = rig.seed(internet_message_id=rig.new_message_id())
        prepared = self._prepare_move(rig, locator, mark_read=True)
        result = rig.port.dispatch(prepared, "k-move-read", {})
        assert result.receipt is not None
        assert result.receipt.items[0].moved_to is not None
        assert not rig.is_unread(result.receipt.items[0].moved_to)

    def test_p14_trash_goes_to_the_recoverable_folder(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        handle = _handle(rig, locator)
        intent = MailTrash.model_validate({"source": rig.source, "messages": [handle]})
        result = rig.port.dispatch(rig.port.prepare(intent, {handle: locator}), "k-trash", {})
        assert result.outcome is DispatchOutcome.APPLIED
        assert result.receipt is not None
        moved_to = result.receipt.items[0].moved_to
        assert moved_to is not None
        assert moved_to != locator
        assert rig.port.resolve(rig.source, moved_to).folder_id == rig.folder_id(FolderRole.TRASH)
        assert rig.copies(message_id, rig.folder_id(FolderRole.TRASH)) == 1
        assert rig.copies(message_id, rig.folder_id(FolderRole.INBOX)) == 0

    def test_p14_a_move_batch_moves_them_all_in_one_dispatch(self, rig: MailRig) -> None:
        inbox = rig.folder_id(FolderRole.INBOX)
        ids = [rig.new_message_id() for _ in range(3)]
        locators = [rig.seed(internet_message_id=i) for i in ids]
        handles = [_handle(rig, loc) for loc in locators]
        gone_locator = rig.seed(internet_message_id=rig.new_message_id())
        gone = _handle(rig, gone_locator)
        refs = {**dict(zip(handles, locators, strict=True)), gone: gone_locator}

        def batch(names: list[str]) -> MailTrash:
            return MailTrash.model_validate(
                {"source": rig.source, "messages": names, "from_folder": inbox}
            )

        prepared = rig.port.prepare(batch([*handles, gone]), refs)
        # One of them leaves before the batch runs: it is reported, the rest still move.
        rig.port.dispatch(rig.port.prepare(batch([gone]), refs), "k-gone", {})
        result = rig.port.dispatch(prepared, "k-batch", {})
        assert result.outcome is DispatchOutcome.APPLIED
        assert result.receipt is not None
        assert [item.ok for item in result.receipt.items] == [True, True, True, False]
        for message_id in ids:
            assert rig.copies(message_id, rig.folder_id(FolderRole.TRASH)) == 1
            assert rig.copies(message_id, inbox) == 0
        assert rig.port.reconcile(prepared, "k-batch").status is ReconcileStatus.PRESENT

    def test_w23_a_filing_batch_moves_marks_read_and_says_where_each_went(
        self, rig: MailRig
    ) -> None:
        inbox, filing = rig.folder_id(FolderRole.INBOX), rig.filing_folder_id()
        ids = [rig.new_message_id() for _ in range(3)]
        locators = [rig.seed(internet_message_id=i) for i in ids]
        handles = [_handle(rig, loc) for loc in locators]
        intent = MailMove.model_validate(
            {
                "source": rig.source,
                "messages": handles,
                "to_folder": filing,
                "mark_read": True,
                "from_folder": inbox,
            }
        )
        prepared = rig.port.prepare(intent, dict(zip(handles, locators, strict=True)))
        result = rig.port.dispatch(prepared, "k-file-batch", {})
        assert result.outcome is DispatchOutcome.APPLIED
        assert result.receipt is not None
        assert all(item.ok for item in result.receipt.items)
        for message_id, item in zip(ids, result.receipt.items, strict=True):
            assert rig.copies(message_id, filing) == 1
            assert rig.copies(message_id, inbox) == 0
            assert item.moved_to is not None
            assert not rig.is_unread(item.moved_to)
        settled = rig.port.reconcile(prepared, "k-file-batch")
        assert settled.status is ReconcileStatus.PRESENT
        assert settled.receipt is not None
        assert [item.locator for item in settled.receipt.items] == locators
        assert all(item.ok and item.moved_to for item in settled.receipt.items)

    def test_w24_marking_many_read_reads_each_folder_once(self, rig: MailRig) -> None:
        locators = [rig.seed(internet_message_id=rig.new_message_id()) for _ in range(12)]
        handles = [_handle(rig, loc) for loc in locators]
        intent = MailMarkRead.model_validate({"source": rig.source, "messages": handles})
        prepared = rig.port.prepare(intent, dict(zip(handles, locators, strict=True)))
        result = rig.port.dispatch(prepared, "k-read-many", {})
        assert result.outcome is DispatchOutcome.APPLIED
        assert result.receipt is not None
        assert all(item.ok for item in result.receipt.items)
        assert not any(rig.is_unread(loc) for loc in locators)
        assert rig.port.reconcile(prepared, "k-read-many").status is ReconcileStatus.PRESENT

    def test_p9_prepare_refuses_a_message_that_is_not_where_the_intent_says(
        self, rig: MailRig
    ) -> None:
        locator = rig.seed(internet_message_id=rig.new_message_id())
        handle = _handle(rig, locator)
        intent = MailMove.model_validate(
            {
                "source": rig.source,
                "messages": [handle],
                "to_folder": rig.filing_folder_id(),
                "from_folder": "a-different-folder",
            }
        )
        with pytest.raises(ChamberlainError) as raised:
            rig.port.prepare(intent, {handle: locator})
        assert raised.value.code is ErrorCode.REMOTE_CHANGED
        assert not raised.value.may_have_happened

    # --- dispatch outcomes and reconcile --------------------------------------

    def test_p10_busy_before_the_command_is_not_applied(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        prepared = self._prepare_move(rig, locator)
        _need(rig, Fault.BUSY_BEFORE_APPLY)
        result = rig.port.dispatch(prepared, "k-busy", {})
        assert result.outcome is DispatchOutcome.NOT_APPLIED
        assert rig.copies(message_id, rig.folder_id(FolderRole.INBOX)) == 1

    def test_p10_a_lost_response_is_unknown_and_reconcile_finds_the_write(
        self, rig: MailRig
    ) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        prepared = self._prepare_move(rig, locator)
        _need(rig, Fault.LOSE_RESPONSE_AFTER_APPLY)
        result = rig.port.dispatch(prepared, "k-lost", {})
        assert result.outcome is DispatchOutcome.UNKNOWN
        assert result.receipt is None
        settled = rig.port.reconcile(prepared, "k-lost")
        assert settled.status is ReconcileStatus.PRESENT
        assert rig.copies(message_id, rig.filing_folder_id()) == 1

    def test_p12_a_write_that_never_landed_reconciles_as_proven_absent(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        prepared = self._prepare_move(rig, locator)
        _need(rig, Fault.LOSE_BEFORE_APPLY)
        assert rig.port.dispatch(prepared, "k-never", {}).outcome is DispatchOutcome.UNKNOWN
        settled = rig.port.reconcile(prepared, "k-never")
        assert settled.status is ReconcileStatus.ABSENT
        assert settled.proof
        assert rig.copies(message_id, rig.folder_id(FolderRole.INBOX)) == 1

    def test_p11_reconcile_is_read_only_and_repeatable(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        prepared = self._prepare_move(rig, locator)
        rig.port.dispatch(prepared, "k-twice", {})
        first = rig.port.reconcile(prepared, "k-twice")
        second = rig.port.reconcile(prepared, "k-twice")
        assert first.status is second.status is ReconcileStatus.PRESENT
        assert rig.copies(message_id) == 1

    def test_p14_a_move_that_leaves_two_copies_is_not_reported_applied(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        prepared = self._prepare_move(rig, locator)
        _need(rig, Fault.DUPLICATE_COPY)
        result = rig.port.dispatch(prepared, "k-dup", {})
        assert result.outcome is DispatchOutcome.UNKNOWN
        assert rig.port.reconcile(prepared, "k-dup").status is ReconcileStatus.UNKNOWN

    def test_p17_a_slow_dispatch_is_unknown_and_may_land_later(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        locator = rig.seed(internet_message_id=message_id)
        prepared = self._prepare_move(rig, locator)
        _need(rig, Fault.SLOW)
        assert rig.port.dispatch(prepared, "k-slow", {}).outcome is DispatchOutcome.UNKNOWN
        rig.advance(LATE)
        assert rig.port.reconcile(prepared, "k-slow").status is ReconcileStatus.PRESENT
        assert rig.copies(message_id, rig.filing_folder_id()) == 1

    # --- send and drafts ------------------------------------------------------

    def test_p15_send_files_one_copy_with_the_given_message_id(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        prepared = self._prepare_send(rig, message_id)
        assert prepared.reconcile_token == message_id
        result = rig.port.dispatch(prepared, "k-send", {})
        assert result.outcome is DispatchOutcome.APPLIED
        assert result.receipt is not None
        assert result.receipt.verification.client_verified
        assert rig.copies(message_id, rig.folder_id(FolderRole.SENT)) == 1
        settled = rig.port.reconcile(prepared, "k-send")
        assert settled.status is ReconcileStatus.PRESENT
        assert settled.receipt is not None
        assert settled.receipt.verification.server_verified

    def test_w8_an_uncertain_send_is_settled_by_reading_not_resending(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        prepared = self._prepare_send(rig, message_id)
        _need(rig, Fault.LOSE_RESPONSE_AFTER_APPLY)
        assert rig.port.dispatch(prepared, "k-send-lost", {}).outcome is DispatchOutcome.UNKNOWN
        for _ in range(2):
            assert rig.port.reconcile(prepared, "k-send-lost").status is ReconcileStatus.PRESENT
        assert rig.copies(message_id, rig.folder_id(FolderRole.SENT)) == 1

    def test_p12_send_absence_is_proven_only_where_declared(self, rig: MailRig) -> None:
        prepared = self._prepare_send(rig, rig.new_message_id())
        _need(rig, Fault.LOSE_BEFORE_APPLY)
        rig.port.dispatch(prepared, "k-send-never", {})
        settled = rig.port.reconcile(prepared, "k-send-never")
        capability = rig.port.capabilities(rig.source).get(CapabilityName.MAIL_SEND)
        assert capability is not None
        declared = IntentKind.MAIL_SEND.value in capability.absence_provable
        assert settled.status is ReconcileStatus.ABSENT
        assert settled.proof == declared

    def test_w3_an_attachment_that_does_not_match_its_hash_is_not_sent(self, rig: MailRig) -> None:
        message_id = rig.new_message_id()
        good = b"the approved bytes"
        attachment = AttachmentRef.model_validate(
            {
                "artifact": "art_0123456789abcdef",
                "sha256": sha256_bytes(good),
                "filename": "approved.txt",
            }
        )
        prepared = self._prepare_send(rig, message_id, (attachment,))
        result = rig.port.dispatch(prepared, "k-attach", {sha256_bytes(good): b"other bytes"})
        assert result.outcome is DispatchOutcome.NOT_APPLIED
        assert rig.copies(message_id) == 0

    def test_p9_draft_update_needs_the_hash_of_the_draft_it_replaces(self, rig: MailRig) -> None:
        first_id = rig.new_message_id()
        compose = {
            "source": rig.source,
            "to": ["owner@example.org"],
            "subject": "Draft",
            "body": "First wording.",
        }
        save = MailDraftSave.model_validate({**compose, "message_id": first_id})
        saved = rig.port.dispatch(rig.port.prepare(save, {}), "k-draft", {})
        assert saved.receipt is not None
        locator = saved.receipt.locator
        raw_hash = saved.receipt.remote_sha256
        assert locator is not None
        assert raw_hash is not None
        handle = _handle(rig, locator)
        changed = {**compose, "body": "Second wording.", "draft": handle}
        stale = MailDraftUpdate.model_validate(
            {**changed, "message_id": rig.new_message_id(), "expected_sha256": "0" * 64}
        )
        with pytest.raises(ChamberlainError) as raised:
            rig.port.prepare(stale, {handle: locator})
        assert raised.value.code is ErrorCode.REMOTE_CHANGED
        second_id = rig.new_message_id()
        update = MailDraftUpdate.model_validate(
            {**changed, "message_id": second_id, "expected_sha256": raw_hash}
        )
        updated = rig.port.dispatch(rig.port.prepare(update, {handle: locator}), "k-draft-2", {})
        assert updated.outcome is DispatchOutcome.APPLIED
        assert updated.receipt is not None
        revised = updated.receipt.locator
        assert revised is not None
        assert revised != locator
        assert rig.port.read(rig.source, revised).body_text == changed["body"]
        drafts = rig.folder_id(FolderRole.DRAFTS)
        assert rig.copies(first_id, drafts) == 0
        assert rig.copies(second_id, drafts) == 1

    def test_w24_mark_read_reports_each_message(self, rig: MailRig) -> None:
        locators = [rig.seed(internet_message_id=rig.new_message_id()) for _ in range(3)]
        refs = {_handle(rig, locator): locator for locator in locators}
        intent = MailMarkRead.model_validate({"source": rig.source, "messages": list(refs)})
        result = rig.port.dispatch(rig.port.prepare(intent, refs), "k-read", {})
        assert result.outcome is DispatchOutcome.APPLIED
        assert result.receipt is not None
        assert [item.ok for item in result.receipt.items] == [True, True, True]
        assert not any(rig.is_unread(locator) for locator in locators)
