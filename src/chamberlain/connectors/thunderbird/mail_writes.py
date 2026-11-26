"""Typed Thunderbird write plans. Each shape dispatches once and reconciles from copy facts."""

from collections.abc import Mapping
from typing import Final

from pydantic import Field

from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.client import typed
from chamberlain.connectors.thunderbird.frames import CHUNK_BYTES
from chamberlain.connectors.thunderbird.identity import (
    encode_locator,
    normalise_body,
    parse_address,
)
from chamberlain.connectors.thunderbird.mail_reads import (
    MailReads,
    ThunderbirdBinding,
    milliseconds,
)
from chamberlain.connectors.thunderbird.protocol import (
    AttachmentSpec,
    Command,
    Fingerprint,
    Header,
    MessageRef,
)
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.domain.intents import (
    IntentKind,
)
from chamberlain.domain.mail import bare, normalise_subject
from chamberlain.domain.operations import (
    DispatchOutcome,
    DispatchResult,
    ItemResult,
    Prepared,
    ProviderReceipt,
    ReconcileResult,
    ReconcileStatus,
    Verification,
)
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_K = IntentKind
DIALOG_ANSWERED: Final = "thunderbird_dialog_answered"
"""Thunderbird asked something on the composer and the add-on answered it at once (safe button)."""
ALLOW_DUPLICATE_REF: Final = "_allow_duplicate"
"""Reserved key in `refs`: the engine sets it to "1" when the grant allows a duplicate send (R3)."""
DISPLAY_ONLY_REF: Final = "_display_only"
"""Reserved key in `refs`: leave a saved draft's composer open (R8) until the intent has a field."""

_PRE_MUTATION_ITEM_CODES: Final = frozenset(
    {
        "not_found",
        "ambiguous",
        "account_mismatch",
        "timeout",
        "busy",
        "bad_request",
        "changed",
        "client_offline",
        "unsupported",
    }
)


class PlanAttachment(Model):
    sha256: str
    filename: str


_BATCH_ITEM_CODES = {
    "not_found": ErrorCode.REQUEST_UNKNOWN_HANDLE,
    "ambiguous": ErrorCode.REMOTE_AMBIGUOUS,
    "changed": ErrorCode.REMOTE_CHANGED,
    "account_mismatch": ErrorCode.SOURCE_NOT_CONFIGURED,
    "client_offline": ErrorCode.SOURCE_UNAVAILABLE,
    "unsupported": ErrorCode.CAPABILITY_UNSUPPORTED,
    "bad_request": ErrorCode.REQUEST_INVALID,
}


class ComposePlan(Model):
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    high_importance: bool
    token: str
    sent_folder_id: str
    draft_folder_id: str
    reply_to: MessageRef | None
    display_only: bool
    attachments: tuple[PlanAttachment, ...]
    fingerprint: Fingerprint
    since_ms: int
    inbox_folder_id: str | None
    draft: MessageRef | None = None
    expected_raw_sha256: str | None = None

    def dispatch(
        self,
        mail: MailReads,
        binding: ThunderbirdBinding,
        prepared: Prepared,
        payloads: Mapping[str, bytes],
    ) -> DispatchResult:
        checked = self._check_payloads(mail, payloads)
        if isinstance(checked, DispatchResult):
            return checked
        specs, files = checked
        common: dict[str, object] = {
            "account_id": binding.account_id,
            "mailbox": binding.mailbox,
            "to": self.to,
            "cc": self.cc,
            "bcc": self.bcc,
            "subject": self.subject,
            "body": self.body,
            "high_importance": self.high_importance,
            "sent_folder_id": self.sent_folder_id,
            "draft_folder_id": self.draft_folder_id,
            "token": self.token,
            "attachments": tuple(specs),
            "reply_to": self.reply_to,
            "display_only": self.display_only,
        }
        mode = prepared.intent_kind.value.removeprefix("mail.")
        reply = mail.client.call(
            Command.COMPOSE,
            protocol.ComposeArgs.model_validate(
                {
                    **common,
                    "mode": mode,
                    "draft": self.draft,
                    "expected_raw_sha256": self.expected_raw_sha256,
                }
            ),
            payloads=files,
        )
        result = typed(reply, protocol.ComposeResult)
        if prepared.intent_kind is _K.MAIL_SEND:
            return DispatchResult(
                outcome=DispatchOutcome.APPLIED,
                receipt=ProviderReceipt(
                    verification=Verification(client_verified=True),
                    warnings=(DIALOG_ANSWERED,) if result.dialogs else (),
                ),
                detail="submitted to Thunderbird" + _dialog_note(result.dialogs),
            )
        if result.header is None:
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail="missing saved draft header"
            )
        # A handle needs the Message-ID: an empty one would name every such draft at once.
        # The draft is saved either way; without one it simply gets no handle.
        locator = (
            encode_locator(
                binding.account_id, result.header.folder_id, result.header.internet_message_id
            )
            if result.header.internet_message_id
            else None
        )
        note = ""
        if result.old_copies_remaining:
            note = "the replaced draft is still present"
        return DispatchResult(
            outcome=DispatchOutcome.APPLIED,
            receipt=ProviderReceipt(
                locator=locator,
                remote_sha256=result.raw_sha256,
                verification=Verification(client_verified=True),
                warnings=(DIALOG_ANSWERED,) if result.dialogs else (),
            ),
            detail=note + _dialog_note(result.dialogs),
        )

    def reconcile(
        self, mail: MailReads, binding: ThunderbirdBinding, prepared: Prepared
    ) -> ReconcileResult:
        kind = prepared.intent_kind
        folders = {"drafts": self.draft_folder_id}
        if kind is _K.MAIL_SEND:
            folders["sent"] = self.sent_folder_id
            if self.inbox_folder_id:
                folders["inbox"] = self.inbox_folder_id
        scopes = [
            protocol.CopyScope(
                folder_id=folder,
                subject=self.subject,
                since_ms=self.since_ms,
                inspect_draft=place == "drafts",
                include_attachment_hashes=True,
            )
            for place, folder in folders.items()
        ]
        places = list(folders)
        if kind is _K.MAIL_SEND:
            places.append("outbox")
            scopes.append(
                protocol.CopyScope(
                    folder_id=None,
                    subject=self.subject,
                    since_ms=self.since_ms,
                    include_attachment_hashes=True,
                )
            )
        if self.draft is not None:
            scopes.append(
                protocol.CopyScope(
                    folder_id=self.draft.folder_id,
                    ids=(self.draft.internet_message_id,),
                    inspect_draft=True,
                )
            )
        result = mail.copies(binding, scopes, refresh=True)
        exact = {
            place: [copy for copy in scope.copies if _matches(copy, self.fingerprint, place)]
            for place, scope in zip(places, result.scopes[: len(places)], strict=True)
        }
        for place in ("sent", "inbox") if kind is _K.MAIL_SEND else ("drafts",):
            found = exact.get(place, [])
            if len(found) > 1:
                return ReconcileResult(
                    status=ReconcileStatus.UNKNOWN, detail="several matching copies"
                )
            if found:
                return self._present(
                    binding, found[0].header, found[0].raw_sha256, result.refresh_verified
                )
        if kind is _K.MAIL_SEND and (exact["drafts"] or exact["outbox"]):
            return ReconcileResult(
                status=ReconcileStatus.QUEUED, detail="waiting in Outbox or Drafts"
            )
        if not all(scope.complete for scope in result.scopes):
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="a folder scan was incomplete"
            )
        if self.draft is not None:
            old = result.scopes[-1].copies
            if len(old) != 1 or old[0].raw_sha256 != self.expected_raw_sha256:
                return ReconcileResult(
                    status=ReconcileStatus.UNKNOWN, detail="the old draft changed"
                )
        return ReconcileResult(
            status=ReconcileStatus.ABSENT,
            proof=result.refresh_verified and mail.absence_provable(prepared.source, kind),
        )

    def _check_payloads(
        self, mail: MailReads, payloads: Mapping[str, bytes]
    ) -> tuple[list[AttachmentSpec], dict[str, bytes]] | DispatchResult:
        """W-3: hashes are checked here, before any byte leaves; a mismatch is `not_applied`."""
        specs: list[AttachmentSpec] = []
        files: dict[str, bytes] = {}
        for index, item in enumerate(self.attachments, start=1):
            data = payloads.get(item.sha256)
            if data is None or sha256_bytes(data) != item.sha256:
                return DispatchResult(
                    outcome=DispatchOutcome.NOT_APPLIED,
                    detail="artifact.hash_mismatch: an attachment does not match its hash",
                )
            if len(data) > mail.limits.max_attachment_bytes:
                return DispatchResult(
                    outcome=DispatchOutcome.NOT_APPLIED, detail="request.too_large: attachment"
                )
            key = f"a{index}"
            chunks = -(-len(data) // CHUNK_BYTES)
            specs.append(
                AttachmentSpec(
                    key=key,
                    name=item.filename,
                    size=len(data),
                    sha256=item.sha256,
                    chunks=chunks,
                )
            )
            files[key] = data
        return specs, files

    def _present(
        self, binding: ThunderbirdBinding, header: Header, raw: str | None, refreshed: bool
    ) -> ReconcileResult:
        receipt = ProviderReceipt(
            locator=encode_locator(
                binding.account_id, header.folder_id, header.internet_message_id
            ),
            remote_sha256=raw,
            verification=Verification(client_verified=True, server_verified=refreshed),
        )
        return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=receipt)

    def check_duplicates(self, mail: MailReads, binding: ThunderbirdBinding) -> None:
        """W-27: block a related send on loose content or incomplete coverage."""
        since = milliseconds(mail.clock.now()) - mail.limits.duplicate_lookback_days * 86_400_000
        scopes = [
            protocol.CopyScope(folder_id=folder, subject=self.subject, since_ms=since)
            for folder in (self.sent_folder_id, self.draft_folder_id, None)
        ]
        facts = mail.copies(binding, scopes)
        wanted = set(self.fingerprint.to + self.fingerprint.cc + self.fingerprint.bcc)
        empty = not normalise_body(self.body)
        for scope in facts.scopes:
            if not scope.complete or any(
                copy.body_sha256 is None
                or copy.body_sha256 == self.fingerprint.body_sha256
                or (
                    empty
                    and wanted.intersection(a.lower() for a in (*copy.to, *copy.cc, *copy.bcc))
                )
                for copy in scope.copies
            ):
                raise ChamberlainError(
                    ErrorCode.REMOTE_CHANGED,
                    "A related message may already be in Sent, Drafts or Outbox.",
                    fix="Check Sent and Drafts. Send again only if the owner allows a duplicate.",
                )


class MoveBaseline(Model):
    source: int
    destination: int
    read: bool | None


class ChangePlan(Model):
    items: tuple[protocol.ChangeGroup, ...]
    before: dict[str, MoveBaseline] = Field(default_factory=dict)
    move_to: str | None = None
    trash: bool = False
    set_read: bool | None = None
    date_window: tuple[int, int] | None = None

    def dispatch(
        self,
        mail: MailReads,
        binding: ThunderbirdBinding,
        prepared: Prepared,
        payloads: Mapping[str, bytes],
    ) -> DispatchResult:
        reply = mail.client.call(
            Command.CHANGE,
            protocol.ChangeArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                items=self.items,
                move_to=self.move_to,
                trash=self.trash,
                set_read=self.set_read,
            ),
        )
        result = typed(reply, protocol.ChangeResult)
        expected = {(group.folder_id, mid) for group in self.items for mid in group.ids}
        seen = {(item.ref.folder_id, bare(item.ref.internet_message_id)) for item in result.items}
        if seen != expected or len(seen) != len(result.items):
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN,
                detail="the batch reply omitted or repeated a message",
            )
        if not any(item.ok for item in result.items) and all(
            item.code in _PRE_MUTATION_ITEM_CODES for item in result.items
        ):
            return DispatchResult(outcome=DispatchOutcome.NOT_APPLIED, detail="no message changed")
        if not result.verified:
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail="the requested change was not verified"
            )
        warnings: list[str] = []
        if result.merged_existing_copy:
            warnings.append("merged_existing_copy")
        if result.read_state_preserved is False:
            warnings.append("read_state_not_preserved")
        return DispatchResult(
            outcome=DispatchOutcome.APPLIED,
            receipt=ProviderReceipt(
                verification=Verification(client_verified=True),
                warnings=tuple(warnings),
                read_state_preserved=result.read_state_preserved,
                items=tuple(
                    _item(
                        binding,
                        item.ref.folder_id,
                        item.ref.internet_message_id,
                        self.move_to,
                        item.ok,
                        item.code,
                    )
                    for item in result.items
                ),
            ),
            detail=", ".join(warnings),
        )

    def reconcile(
        self, mail: MailReads, binding: ThunderbirdBinding, prepared: Prepared
    ) -> ReconcileResult:
        result = self.facts(mail, binding, refresh=True)
        if not all(scope.complete for scope in result.scopes):
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="a folder scan was incomplete"
            )
        destination = by_id(result.scopes[-1]) if self.move_to else {}
        proof = result.refresh_verified and mail.absence_provable(
            prepared.source, prepared.intent_kind
        )
        items: list[ItemResult] = []
        pending = False
        merged = False
        preserved = True
        for group, scope in zip(self.items, result.scopes[: len(self.items)], strict=True):
            source = by_id(scope)
            for mid in group.ids:
                here, there = source.get(mid, []), destination.get(mid, [])
                if self.move_to is None:
                    if len(here) != 1:
                        return ReconcileResult(
                            status=ReconcileStatus.UNKNOWN, detail="a message is missing or doubled"
                        )
                    done = here[0].read == self.set_read
                    pending |= not done
                else:
                    before = self.before[mid]
                    eligible = before.source == 1 and (self.trash or before.destination == 0)
                    unchanged = len(here) == before.source and len(there) == before.destination
                    did_merge = (
                        self.trash and before.destination > 0 and len(there) == before.destination
                    )
                    done = (
                        eligible
                        and not here
                        and (len(there) == before.destination + 1 or did_merge)
                    )
                    if not done and not unchanged:
                        return ReconcileResult(
                            status=ReconcileStatus.UNKNOWN,
                            detail="the change in copy counts does not prove the move",
                        )
                    pending |= eligible and unchanged
                    merged |= done and did_merge
                    if done and self.set_read and any(not h.read for h in there):
                        return ReconcileResult(
                            status=ReconcileStatus.UNKNOWN, detail="filed flags differ"
                        )
                    if done and not self.set_read:
                        preserved &= before.read is not None and all(
                            copy.read == before.read for copy in there
                        )
                items.append(
                    _item(
                        binding,
                        group.folder_id,
                        mid,
                        self.move_to,
                        done,
                        "ambiguous" if here else "not_found",
                    )
                )
        if not any(item.ok for item in items):
            return ReconcileResult(status=ReconcileStatus.ABSENT, proof=proof)
        if pending:
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="only some messages changed"
            )
        return ReconcileResult(
            status=ReconcileStatus.PRESENT,
            receipt=ProviderReceipt(
                verification=Verification(
                    client_verified=True, server_verified=result.refresh_verified
                ),
                warnings=tuple(
                    code
                    for condition, code in (
                        (merged, "merged_existing_copy"),
                        (not preserved, "read_state_not_preserved"),
                    )
                    if condition
                ),
                read_state_preserved=preserved if self.move_to else None,
                items=tuple(items),
            ),
        )

    def facts(
        self, mail: MailReads, binding: ThunderbirdBinding, *, refresh: bool
    ) -> protocol.CopiesResult:
        since, until = self.date_window if refresh and self.date_window else (None, None)
        scopes = [
            protocol.CopyScope(
                folder_id=group.folder_id, ids=group.ids, since_ms=since, until_ms=until
            )
            for group in self.items
        ]
        if self.move_to is not None:
            scopes.append(
                protocol.CopyScope(
                    folder_id=self.move_to, ids=self.items[0].ids, since_ms=since, until_ms=until
                )
            )
        return mail.copies(binding, scopes, refresh=refresh)


class DiscardPlan(Model):
    draft: MessageRef
    expected_raw_sha256: str

    def dispatch(
        self,
        mail: MailReads,
        binding: ThunderbirdBinding,
        prepared: Prepared,
        payloads: Mapping[str, bytes],
    ) -> DispatchResult:
        reply = mail.client.call(
            Command.DRAFT_DISCARD,
            protocol.DraftDiscardArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                draft=self.draft,
                expected_raw_sha256=self.expected_raw_sha256,
            ),
        )
        result = typed(reply, protocol.DiscardResult)
        return DispatchResult(
            outcome=DispatchOutcome.APPLIED,
            receipt=ProviderReceipt(verification=Verification(client_verified=True)),
            detail=f"closed {result.closed} composer(s); saved mail untouched",
        )

    def reconcile(
        self, mail: MailReads, binding: ThunderbirdBinding, prepared: Prepared
    ) -> ReconcileResult:
        scope = mail.copies(
            binding,
            [
                protocol.CopyScope(
                    folder_id=self.draft.folder_id,
                    ids=(self.draft.internet_message_id,),
                    inspect_draft=True,
                )
            ],
        ).scopes[0]
        if not scope.complete or len(scope.copies) != 1:
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="draft not uniquely identified"
            )
        copy = scope.copies[0]
        if copy.raw_sha256 != self.expected_raw_sha256 or copy.composers_open is None:
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="could not verify the draft"
            )
        if copy.composers_open == 0:
            return ReconcileResult(
                status=ReconcileStatus.PRESENT,
                receipt=ProviderReceipt(verification=Verification(client_verified=True)),
            )
        return ReconcileResult(
            status=ReconcileStatus.ABSENT,
            proof=mail.absence_provable(prepared.source, prepared.intent_kind),
        )


def _item(
    binding: ThunderbirdBinding, folder: str, mid: str, dest: str | None, ok: bool, code: str
) -> ItemResult:
    return ItemResult(
        locator=encode_locator(binding.account_id, folder, mid),
        ok=ok,
        moved_to=encode_locator(binding.account_id, dest, mid) if ok and dest else None,
        code=None if ok else _BATCH_ITEM_CODES.get(code, ErrorCode.SOURCE_UNAVAILABLE),
    )


def by_id(scope: protocol.CopyScopeResult) -> dict[str, list[Header]]:
    by_id: dict[str, list[Header]] = {}
    for copy in scope.copies:
        by_id.setdefault(bare(copy.header.internet_message_id), []).append(copy.header)
    return by_id


def _matches(copy: protocol.CopyFact, want: Fingerprint, place: str) -> bool:
    def addresses(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(value.lower() for value in values))

    return (
        parse_address(copy.header.author).address.lower() == want.sender.lower()
        and normalise_subject(copy.subject or "") == normalise_subject(want.subject)
        and copy.body_sha256 == want.body_sha256
        and addresses(copy.to) == want.to
        and addresses(copy.cc) == want.cc
        and (place == "inbox" or addresses(copy.bcc) == want.bcc)
        and tuple(sorted(copy.attachment_sha256)) == want.attachment_sha256
        and copy.high_importance == want.high_importance
        and (not want.in_reply_to or want.in_reply_to in copy.in_reply_to)
    )


def _dialog_note(dialogs: tuple[str, ...]) -> str:
    if not dialogs:
        return ""
    return "; Thunderbird asked, and the add-on answered at once: " + " | ".join(
        d[:200] for d in dialogs
    )
