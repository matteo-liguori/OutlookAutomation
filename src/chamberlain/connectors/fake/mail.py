"""In-memory MailPort, shaped like the Thunderbird bridge.

Folders, session-scoped integer IDs that change on restart, and stable
locators (`<folder id>/<Message-ID>`) that do not.

Contract: docs/contracts/08-ports.md.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast

from pydantic import JsonValue

from chamberlain.connectors.fake.world import FakeWorld, Fault, StoredMail, Submission, text
from chamberlain.domain.canonical import sha256_bytes, sha256_text
from chamberlain.domain.capabilities import (
    Capability,
    CapabilityName,
    CapabilitySet,
    SourceHealth,
    VerificationLevel,
)
from chamberlain.domain.intents import (
    IntentKind,
    MailDraftDiscard,
    MailDraftSave,
    MailDraftUpdate,
    MailIntent,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
)
from chamberlain.domain.mail import (
    Address,
    AttachmentBytes,
    AttachmentMeta,
    Folder,
    FolderRole,
    MailQuery,
    Message,
    MessageSummary,
)
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
from chamberlain.ports.page import Gap, Page
from chamberlain.ports.sync import FOLDER_SECONDS, FolderOutcome, SyncReport, run_sync
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_K = IntentKind
_VERIFIED = Verification(client_verified=True, server_verified=True)
_CLIENT_ONLY = Verification(client_verified=True, server_verified=False)
_DEFAULT_FOLDERS = (
    Folder(id="f-inbox", name="Inbox", path="/Inbox", role=FolderRole.INBOX),
    Folder(id="f-sent", name="Sent Items", path="/Sent Items", role=FolderRole.SENT),
    Folder(id="f-drafts", name="Drafts", path="/Drafts", role=FolderRole.DRAFTS),
    Folder(id="f-trash", name="Deleted Items", path="/Deleted Items", role=FolderRole.TRASH),
    Folder(id="f-archive", name="Archive", path="/Archive", role=FolderRole.ARCHIVE),
)
_ALWAYS_PROVABLE = frozenset(
    {
        _K.MAIL_DRAFT_SAVE.value,
        _K.MAIL_DRAFT_UPDATE.value,
        _K.MAIL_DRAFT_DISCARD.value,
        _K.MAIL_MOVE.value,
        _K.MAIL_TRASH.value,
        _K.MAIL_MARK_READ.value,
    }
)


def _texts(data: Mapping[str, JsonValue], key: str) -> list[str]:
    value = data[key]
    if not isinstance(value, list):
        raise TypeError(f"prepared field {key} is not a list")
    return [str(item) for item in value]


class FakeMail:
    """Implements `chamberlain.ports.MailPort` over a `FakeWorld`."""

    def __init__(self, world: FakeWorld, *, send_absence_provable: bool = False) -> None:
        self._world = world
        self._send_absence_provable = send_absence_provable

    # --- seeding (tests only) -------------------------------------------------

    def add_source(self, source: str) -> None:
        self._world.mail.setdefault(source, [])
        self._world.folders.setdefault(source, list(_DEFAULT_FOLDERS))

    def add_folder(
        self, source: str, folder_id: str, name: str, role: FolderRole = FolderRole.OTHER
    ) -> Folder:
        folder = Folder(id=folder_id, name=name, path="/" + name, role=role)
        self._world.folders[source].append(folder)
        return folder

    def add_message(
        self,
        source: str,
        *,
        folder_id: str = "f-inbox",
        internet_message_id: str,
        subject: str = "Hello",
        sender: str = "alice@example.org",
        to: tuple[str, ...] = ("owner@example.org",),
        cc: tuple[str, ...] = (),
        authentication_results: tuple[str, ...] = (),
        body: str = "Body text.",
        unread: bool = True,
        date: datetime | None = None,
        attachments: tuple[tuple[str, bytes], ...] = (),
        in_reply_to: str = "",
        references: tuple[str, ...] = (),
        unsynced: bool = False,
    ) -> str:
        """Store one physical copy and return its stable locator.

        `unsynced` models a client that has not synced the folder yet: the message exists but
        no search sees it until `sync_folders` has finished that folder."""
        world = self._world
        stored = StoredMail(
            uid=world.next_uid(),
            session_id=world.next_session_id(),
            folder_id=world.folder(source, folder_id).id,
            internet_message_id=internet_message_id,
            subject=subject,
            sender=Address(address=sender),
            to=tuple(Address(address=a) for a in to),
            cc=tuple(Address(address=a) for a in cc),
            date=date or world.clock.now(),
            unread=unread,
            body=body,
            authentication_results=authentication_results,
            in_reply_to=in_reply_to,
            references=references,
            unsynced=unsynced,
            attachments=[
                (
                    AttachmentMeta(
                        id=f"a{index}",
                        filename=name,
                        media_type="application/octet-stream",
                        size=len(data),
                    ),
                    data,
                )
                for index, (name, data) in enumerate(attachments, start=1)
            ],
        )
        world.mail[source].append(stored)
        return self._locator(stored)

    def deliver_self_sends(self, source: str, on: bool = True) -> None:
        """Make a send to the mailbox's own address also arrive in its Inbox."""
        (self._world.delivers_to_self.add if on else self._world.delivers_to_self.discard)(source)

    def set_address(self, source: str, address: str) -> None:
        """The mailbox's own address: its sender, and where mail to it is delivered."""
        self._world.mail_addresses[source] = address.lower()

    def act_like_exchange(self, source: str, on: bool = True) -> None:
        """The server saves its own Sent copy of every message it accepts."""
        (self._world.server_keeps_sent.add if on else self._world.server_keeps_sent.discard)(source)

    def set_sent_copy(self, source: str, mode: str) -> None:
        """`client` (default): the client files the Sent copy. `server`: it files none."""
        self._world.sent_copy[source] = mode

    def composers(self, source: str) -> tuple[int, int]:
        """Read-only, as the Thunderbird bridge's status: (the harness's, the owner's)."""
        self._world.require_source(self._world.mail, source)
        return self._world.open_composers, len(self._world.owner_composers)

    def sent_copies(self, source: str, internet_message_id: str) -> int:
        """Copies of one message in the source's Sent folder."""
        folder = self._world.folder_by_role(source, FolderRole.SENT).id
        return len(self._matching(source, internet_message_id, folder))

    def stick_folder(self, source: str, folder_id: str, stuck: bool = True) -> None:
        """Make a folder's sync never finish (or finish again)."""
        folders = self._world.stuck_folders.setdefault(source, set())
        if stuck:
            folders.add(folder_id)
        else:
            folders.discard(folder_id)

    def copies(self, source: str, internet_message_id: str, folder_id: str | None = None) -> int:
        """How many physical copies exist, optionally in one folder."""
        return len(self._matching(source, internet_message_id, folder_id))

    def is_unread(self, source: str, locator: str) -> bool:
        return self._one(source, locator).unread

    def session_id(self, source: str, locator: str) -> int:
        return self._one(source, locator).session_id

    # --- port: description ----------------------------------------------------

    def capabilities(self, source: str) -> CapabilitySet:
        self._world.require_source(self._world.mail, source)
        provable = set(_ALWAYS_PROVABLE)
        if self._send_absence_provable:
            provable.add(_K.MAIL_SEND.value)
        limits = {"max_attachment_bytes": 25 * 1024 * 1024, "max_recipients": 100, "page_size": 200}
        names = [name for name in CapabilityName if name.value.startswith("mail.")]
        return CapabilitySet.model_validate(
            {
                "source": source,
                "capabilities": [
                    Capability(
                        name=name,
                        level=VerificationLevel.IMPLEMENTED,
                        limits=limits,
                        absence_provable=frozenset(provable),
                    )
                    for name in names
                ],
            }
        )

    def health(self, source: str) -> SourceHealth:
        self._world.require_source(self._world.mail, source)
        return self._world.health(source)

    # --- port: FolderSync -----------------------------------------------------

    def sync_folders(
        self,
        source: str,
        folder_ids: Sequence[str],
        time_limit: timedelta,
        retry: Collection[str] = frozenset(),
    ) -> SyncReport:
        self._world.require_source(self._world.mail, source)
        self._world.sync_log.append((source, tuple(folder_ids)))

        def attempt(group: Sequence[str]) -> list[FolderOutcome]:
            stuck = self._world.stuck_folders.get(source, set())
            outcomes: list[FolderOutcome] = []
            for folder_id in group:
                if folder_id in stuck:
                    self._world.clock.advance(seconds=FOLDER_SECONDS)
                    outcomes.append(self._outcome(folder_id, False, "sync-callback-timeout"))
                    continue
                for stored in self._world.mail[source]:
                    if stored.folder_id == folder_id:
                        stored.unsynced = False
                outcomes.append(self._outcome(folder_id, True, ""))
            return outcomes

        return run_sync(attempt, folder_ids, self._world.clock, time_limit, retry)

    def _outcome(self, folder_id: str, synced: bool, code: str) -> FolderOutcome:
        return FolderOutcome(
            folder_id=folder_id, synced=synced, code=code, at=self._world.clock.now()
        )

    # --- port: reading --------------------------------------------------------

    def folders(self, source: str) -> list[Folder]:
        self._begin_read(source)
        return list(self._world.folders[source])

    def search(
        self, source: str, query: MailQuery, cursor: str | None = None
    ) -> Page[MessageSummary]:
        self._begin_read(source)
        ranged = [
            m
            for m in self._world.mail[source]
            if not m.unsynced and self._matches(m, query.model_copy(update={"text": None}))
        ]
        cap = self._world.scan_cap.get(source)
        capped = cap is not None and len(ranged) > cap
        if cap is not None:
            ranged = ranged[:cap]
        matches = [m for m in ranged if self._matches(m, query)]
        matches.sort(key=lambda m: (m.date, m.uid), reverse=True)
        page = self._world.page(
            Page[MessageSummary],
            source,
            [self._summary(source, m) for m in matches],
            cursor,
            query.limit,
            "mail store",
        )
        if not capped:
            return page
        gap = Gap(source=source, reason="The search stopped at its scan limit.", narrow=True)
        return page.model_copy(update={"gaps": (*page.gaps, gap), "complete": False})

    def read(self, source: str, locator: str) -> Message:
        self._begin_read(source)
        stored = self._one(source, locator)
        return Message.model_validate(
            {
                **self._summary(source, stored).model_dump(),
                "to": stored.to,
                "cc": stored.cc,
                "body_text": stored.body,
                "raw_sha256": stored.raw_sha256,
                "authentication_results": stored.authentication_results,
                "attachments": [meta for meta, _ in stored.attachments],
            }
        )

    def attachment(self, source: str, locator: str, attachment_id: str) -> AttachmentBytes:
        self._begin_read(source)
        for meta, data in self._one(source, locator).attachments:
            if meta.id == attachment_id:
                return AttachmentBytes(meta=meta, data=data, sha256=sha256_bytes(data))
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such attachment.")

    def resolve(self, source: str, locator: str) -> MessageSummary:
        self._begin_read(source)
        return self._summary(source, self._one(source, locator))

    def folder_of(self, source: str, locator: str) -> str:
        return locator.split("/", 1)[0]

    # --- port: writing --------------------------------------------------------

    def prepare(self, intent: MailIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only (P-9)."""
        source = intent.source
        self._begin_read(source)
        data: dict[str, JsonValue]
        token: str | None = None
        match intent:
            case MailSend() | MailDraftSave() | MailDraftUpdate():
                data = self._compose_data(intent)
                if isinstance(intent, MailSend):
                    data["sent_copy"] = self._world.sent_copy.get(source, "client")
                data["display_only"] = refs.get("_display_only") == "1"
                token = intent.message_id
                if token is None:
                    raise ChamberlainError(
                        ErrorCode.REQUEST_INVALID,
                        "The message has no Message-ID.",
                        field="message_id",
                    )
                if intent.reply_to is not None:
                    data["reply_locator"] = self._locator(self._one(source, refs[intent.reply_to]))
                if isinstance(intent, MailDraftUpdate):
                    draft = self._draft(source, refs[intent.draft], intent.expected_sha256)
                    data["draft_locator"] = self._locator(draft)
                    data["expected_sha256"] = intent.expected_sha256
            case MailDraftDiscard():
                draft = self._draft(source, refs[intent.draft], intent.expected_sha256)
                data = {
                    "draft_locator": self._locator(draft),
                    "expected_sha256": intent.expected_sha256,
                }
            case MailMove() | MailTrash():
                # A batch: each message is checked where it is moved, as Thunderbird does.
                locators = [refs[h] for h in intent.messages]
                folders = {locator.split("/", 1)[0] for locator in locators}
                if len(folders) != 1 or (intent.from_folder and intent.from_folder not in folders):
                    raise ChamberlainError(
                        ErrorCode.REMOTE_CHANGED, "The messages are not in that folder."
                    )
                filing = isinstance(intent, MailMove)
                target = (
                    intent.to_folder
                    if isinstance(intent, MailMove)
                    else self._world.folder_by_role(source, FolderRole.TRASH).id
                )
                if target in folders:
                    raise ChamberlainError(
                        ErrorCode.REMOTE_CHANGED, "The messages are already in that folder."
                    )
                if target not in {folder.id for folder in self._world.folders[source]}:
                    raise ChamberlainError(
                        ErrorCode.REQUEST_INVALID, "The destination folder does not exist."
                    )
                matches = [self._find(source, locator) for locator in locators]
                movable = any(
                    len(found) == 1
                    and (
                        not filing
                        or not self._matching(source, found[0].internet_message_id, target)
                    )
                    for found in matches
                )
                if not movable:
                    if not any(matches):
                        raise ChamberlainError(
                            ErrorCode.REQUEST_UNKNOWN_HANDLE,
                            "None of the messages is in the folder.",
                        )
                    raise ChamberlainError(
                        ErrorCode.REMOTE_AMBIGUOUS, "No message could be moved unambiguously."
                    )
                data = {
                    "locators": cast(list[JsonValue], locators),
                    "from_folder": intent.from_folder or "",
                    "to_folder": target,
                    "filing": filing,
                    "mark_read": isinstance(intent, MailMove) and intent.mark_read,
                }
            case MailMarkRead():
                locators = [self._locator(self._one(source, refs[h])) for h in intent.messages]
                data = {"locators": cast(list[JsonValue], locators), "read": intent.read}
        return Prepared.model_validate(
            {"source": source, "intent_kind": intent.kind, "reconcile_token": token, "data": data}
        )

    def dispatch(
        self, prepared: Prepared, key: str, payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        """The one mutation (P-10). Never retries and never raises."""
        kind = prepared.intent_kind

        def finish(receipt: ProviderReceipt) -> DispatchResult:
            if kind in {_K.MAIL_MOVE, _K.MAIL_TRASH} and not self._move_settled(prepared):
                return DispatchResult(
                    outcome=DispatchOutcome.UNKNOWN, detail="fake: move not verified"
                )
            if receipt.items and not any(item.ok for item in receipt.items):
                return DispatchResult(
                    outcome=DispatchOutcome.NOT_APPLIED, detail="fake: none moved"
                )
            return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=receipt)

        return self._world.run_dispatch(
            prepared, key, lambda: self._apply(prepared, payloads), finish
        )

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11). Needs only `prepared`."""
        source = prepared.source
        world = self._world
        if (gated := world.reconcile_gate("store")) is not None:
            return gated
        data = prepared.data
        kind = prepared.intent_kind
        provable = kind.value in _ALWAYS_PROVABLE or (
            kind is _K.MAIL_SEND and self._send_absence_provable
        )
        absent = ReconcileResult(status=ReconcileStatus.ABSENT, proof=provable)
        unknown = ReconcileResult(status=ReconcileStatus.UNKNOWN)
        if kind in {_K.MAIL_SEND, _K.MAIL_DRAFT_SAVE}:
            role = FolderRole.SENT if kind is _K.MAIL_SEND else FolderRole.DRAFTS
            folder = world.folder_by_role(source, role).id
            found = self._matching(source, prepared.reconcile_token or "", folder)
            if len(found) > 1:
                return unknown
            if not found and prepared.reconcile_token in world.outbox.get(source, set()):
                return ReconcileResult(status=ReconcileStatus.QUEUED, detail="fake: in Outbox")
            return self._present(found[0]) if found else absent
        if kind is _K.MAIL_DRAFT_UPDATE:
            folder = world.folder_by_role(source, FolderRole.DRAFTS).id
            found = self._matching(source, prepared.reconcile_token or "", folder)
            if found:
                return self._present(found[0])
            return absent if self._find(source, text(data, "draft_locator")) else unknown
        if kind is _K.MAIL_DRAFT_DISCARD:
            still = self._find(source, text(data, "draft_locator"))
            if not still:
                return ReconcileResult(
                    status=ReconcileStatus.PRESENT, receipt=ProviderReceipt(verification=_VERIFIED)
                )
            return absent
        if kind in {_K.MAIL_MOVE, _K.MAIL_TRASH}:
            # As Thunderbird: moved = gone from the folder and at the destination; stuck =
            # still in the folder with no copy at the destination.
            target = text(data, "to_folder")
            moved = stuck = 0
            items: list[ItemResult] = []
            for locator in _texts(data, "locators"):
                message_id = locator.split("/", 1)[1]
                there = self._matching(source, message_id, target)
                here = self._find(source, locator)
                if data["filing"] is True and len(there) > 1:
                    return unknown
                moved += bool(there and not here)
                stuck += bool(here and not there)
                if there and not here:
                    items.append(
                        ItemResult(locator=locator, ok=True, moved_to=self._locator(there[0]))
                    )
                if data["mark_read"] is True and there and not here and there[0].unread:
                    return unknown
            if not moved:
                return absent if stuck == len(_texts(data, "locators")) else unknown
            if moved != len(_texts(data, "locators")):
                return unknown
            return ReconcileResult(
                status=ReconcileStatus.PRESENT,
                receipt=ProviderReceipt(verification=_VERIFIED, items=tuple(items)),
            )
        wanted_unread = not bool(data["read"])
        states = [self._find(source, locator) for locator in _texts(data, "locators")]
        if any(len(found) != 1 for found in states):
            return unknown
        if all(found[0].unread == wanted_unread for found in states):
            return ReconcileResult(
                status=ReconcileStatus.PRESENT, receipt=ProviderReceipt(verification=_VERIFIED)
            )
        return absent

    # --- the mutations --------------------------------------------------------

    def _apply(self, prepared: Prepared, payloads: Mapping[str, bytes]) -> ProviderReceipt:
        source = prepared.source
        data = prepared.data
        kind = prepared.intent_kind
        world = self._world
        if kind in {_K.MAIL_SEND, _K.MAIL_DRAFT_SAVE, _K.MAIL_DRAFT_UPDATE}:
            attachments = self._attachments(data, payloads)
            if kind is _K.MAIL_DRAFT_UPDATE:
                old = self._draft(
                    source, text(data, "draft_locator"), text(data, "expected_sha256")
                )
                world.mail[source].remove(old)
            role = FolderRole.SENT if kind is _K.MAIL_SEND else FolderRole.DRAFTS
            stored = StoredMail(
                uid=world.next_uid(),
                session_id=world.next_session_id(),
                folder_id=world.folder_by_role(source, role).id,
                internet_message_id=prepared.reconcile_token or "",
                subject=text(data, "subject"),
                sender=Address(address=self._address(source)),
                to=tuple(Address(address=a) for a in _texts(data, "to")),
                cc=tuple(Address(address=a) for a in _texts(data, "cc")),
                date=world.clock.now(),
                unread=False,
                body=text(data, "body"),
                attachments=attachments,
                raw_sha256=text(data, "raw_sha256"),
            )
            if kind is _K.MAIL_SEND:
                stored = self._submit(source, stored, data)
            else:
                world.mail[source].append(stored)
                if data.get("display_only") is True:
                    world.open_composers += 1
            if (
                kind is _K.MAIL_SEND
                and source in world.delivers_to_self
                and any(a.address.lower() == stored.sender.address.lower() for a in stored.to)
            ):
                world.mail[source].append(
                    replace(
                        stored,
                        uid=world.next_uid(),
                        folder_id=world.folder_by_role(source, FolderRole.INBOX).id,
                        unread=True,
                    )
                )
            verification = _CLIENT_ONLY if kind is _K.MAIL_SEND else _VERIFIED
            return ProviderReceipt(
                locator=self._locator(stored),
                remote_sha256=stored.raw_sha256,
                verification=verification,
            )
        if kind is _K.MAIL_DRAFT_DISCARD:
            old = self._draft(source, text(data, "draft_locator"), text(data, "expected_sha256"))
            world.mail[source].remove(old)
            return ProviderReceipt(verification=_VERIFIED)
        if kind in {_K.MAIL_MOVE, _K.MAIL_TRASH}:
            target = text(data, "to_folder")
            moved: list[ItemResult] = []
            for locator in _texts(data, "locators"):
                found = self._find(source, locator)
                filed_already = data["filing"] is True and bool(
                    found and self._matching(source, found[0].internet_message_id, target)
                )
                if len(found) == 1 and not filed_already:
                    if world.take(Fault.DUPLICATE_COPY):
                        world.mail[source].append(replace(found[0], uid=world.next_uid()))
                    found[0].folder_id = target
                    if data["mark_read"] is True:
                        found[0].unread = False
                    moved.append(
                        ItemResult(locator=locator, ok=True, moved_to=self._locator(found[0]))
                    )
                else:
                    code = ErrorCode.REMOTE_AMBIGUOUS if found else ErrorCode.REQUEST_UNKNOWN_HANDLE
                    moved.append(ItemResult(locator=locator, ok=False, code=code))
            return ProviderReceipt(verification=_VERIFIED, items=tuple(moved))
        results: list[ItemResult] = []
        for locator in _texts(data, "locators"):
            found = self._find(source, locator)
            if len(found) == 1:
                found[0].unread = not bool(data["read"])
                results.append(ItemResult(locator=locator, ok=True))
            else:
                code = ErrorCode.REMOTE_AMBIGUOUS if found else ErrorCode.REQUEST_UNKNOWN_HANDLE
                results.append(ItemResult(locator=locator, ok=False, code=code))
        return ProviderReceipt(verification=_VERIFIED, items=tuple(results))

    def _submit(
        self, source: str, client_copy: StoredMail, data: Mapping[str, JsonValue]
    ) -> StoredMail:
        """One submission: the Sent copies the client and the server file, and delivery to
        other fake mailboxes. Returns the Sent copy the receipt names."""
        world = self._world
        recipients = tuple(
            a.lower() for a in (*_texts(data, "to"), *_texts(data, "cc"), *_texts(data, "bcc"))
        )
        world.submissions.append(Submission(source, client_copy.internet_message_id, recipients))
        copies: list[StoredMail] = []
        if data.get("sent_copy", "client") == "client":
            copies.append(client_copy)
        if source in world.server_keeps_sent:
            copies.append(
                replace(client_copy, uid=world.next_uid(), session_id=world.next_session_id())
            )
        world.mail[source].extend(copies)
        for other, address in world.mail_addresses.items():
            if other != source and address in recipients and other in world.mail:
                world.mail[other].append(
                    replace(
                        client_copy,
                        uid=world.next_uid(),
                        session_id=world.next_session_id(),
                        folder_id=world.folder_by_role(other, FolderRole.INBOX).id,
                        unread=True,
                    )
                )
        return copies[0] if copies else client_copy

    def _address(self, source: str) -> str:
        return self._world.mail_addresses.get(source, "owner@example.org")

    def _move_settled(self, prepared: Prepared) -> bool:
        """Exactly one copy at the destination and none at the source (P-14)."""
        data = prepared.data
        for locator in _texts(data, "locators"):
            message_id = locator.split("/", 1)[1]
            here = self._find(prepared.source, locator)
            there = self._matching(prepared.source, message_id, text(data, "to_folder"))
            if here and there:
                return False
        return True

    # --- helpers --------------------------------------------------------------

    def _begin_read(self, source: str) -> None:
        self._world.require_source(self._world.mail, source)
        self._world.begin_read(source)

    @staticmethod
    def _locator(stored: StoredMail) -> str:
        return f"{stored.folder_id}/{stored.internet_message_id}"

    def _find(self, source: str, locator: str) -> list[StoredMail]:
        return [m for m in self._world.mail[source] if self._locator(m) == locator]

    def _one(self, source: str, locator: str) -> StoredMail:
        found = self._find(source, locator)
        if not found:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That message is not there.")
        if len(found) > 1:
            raise ChamberlainError(ErrorCode.REMOTE_AMBIGUOUS, "More than one copy matches.")
        return found[0]

    def _matching(
        self, source: str, internet_message_id: str, folder_id: str | None
    ) -> list[StoredMail]:
        return [
            m
            for m in self._world.mail[source]
            if m.internet_message_id == internet_message_id
            and (folder_id is None or m.folder_id == folder_id)
        ]

    def _draft(self, source: str, locator: str, expected_sha256: str) -> StoredMail:
        draft = self._one(source, locator)
        if draft.folder_id != self._world.folder_by_role(source, FolderRole.DRAFTS).id:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "That message is not a draft.")
        if draft.raw_sha256 != expected_sha256:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The draft changed since it was read.")
        return draft

    @staticmethod
    def _compose_data(intent: MailSend | MailDraftSave | MailDraftUpdate) -> dict[str, JsonValue]:
        attachments: list[JsonValue] = [
            {"sha256": a.sha256, "filename": a.filename} for a in intent.attachments
        ]
        data: dict[str, JsonValue] = {
            "to": list(intent.to),
            "cc": list(intent.cc),
            "bcc": list(intent.bcc),
            "subject": intent.subject,
            "body": intent.body,
            "attachments": attachments,
        }
        hashed = "\n".join(
            [",".join(intent.to), ",".join(intent.cc), ",".join(intent.bcc), intent.subject]
            + [intent.body]
            + [a.sha256 for a in intent.attachments]
        )
        data["raw_sha256"] = sha256_text(hashed)
        return data

    @staticmethod
    def _attachments(
        data: Mapping[str, JsonValue], payloads: Mapping[str, bytes]
    ) -> list[tuple[AttachmentMeta, bytes]]:
        result: list[tuple[AttachmentMeta, bytes]] = []
        listed = data["attachments"]
        if not isinstance(listed, list):
            raise TypeError("prepared attachments are not a list")
        for index, entry in enumerate(listed, start=1):
            if not isinstance(entry, dict):
                raise TypeError("prepared attachment is not an object")
            digest = str(entry["sha256"])
            content = payloads.get(digest)
            if content is None or sha256_bytes(content) != digest:
                raise ChamberlainError(
                    ErrorCode.ARTIFACT_HASH_MISMATCH, "An attachment does not match its hash."
                )
            meta = AttachmentMeta(
                id=f"a{index}",
                filename=str(entry["filename"]),
                media_type="application/octet-stream",
                size=len(content),
            )
            result.append((meta, content))
        return result

    def _present(self, stored: StoredMail) -> ReconcileResult:
        receipt = ProviderReceipt(
            locator=self._locator(stored), remote_sha256=stored.raw_sha256, verification=_VERIFIED
        )
        return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=receipt)

    def _summary(self, source: str, stored: StoredMail) -> MessageSummary:
        return MessageSummary.model_validate(
            {
                "source": source,
                "locator": self._locator(stored),
                "folder_id": stored.folder_id,
                "internet_message_id": stored.internet_message_id,
                "subject": stored.subject,
                "sender": stored.sender,
                "date": stored.date,
                "unread": stored.unread,
                "has_attachments": bool(stored.attachments),
                "preview": stored.body[:120],
                "to": stored.to,
                "cc": stored.cc,
                "in_reply_to": stored.in_reply_to,
                "references": stored.references,
            }
        )

    @staticmethod
    def _matches(stored: StoredMail, query: MailQuery) -> bool:
        if query.folder_id is not None and stored.folder_id != query.folder_id:
            return False
        if query.internet_message_id not in (None, stored.internet_message_id):
            return False
        if query.unread is not None and stored.unread != query.unread:
            return False
        if query.since is not None and stored.date < query.since:
            return False
        if query.until is not None and stored.date > query.until:
            return False
        if query.text:
            haystack = f"{stored.subject}\n{stored.body}\n{stored.sender.address}".casefold()
            return query.text.casefold() in haystack
        return True
