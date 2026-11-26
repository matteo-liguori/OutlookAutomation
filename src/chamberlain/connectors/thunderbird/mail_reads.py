"""Thunderbird mail reads, synchronization and bounded copy observations."""

import base64
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, TypedDict

from pydantic import Field

from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.client import BridgeClient, BridgeFailure, typed
from chamberlain.connectors.thunderbird.identity import (
    MessageLocator,
    bracketed,
    decode_locator,
    emails_only,
    encode_locator,
    parse_address,
)
from chamberlain.connectors.thunderbird.protocol import (
    Command,
    CopyScope,
    Header,
)
from chamberlain.domain.canonical import sha256_bytes, sha256_text
from chamberlain.domain.capabilities import (
    Capability,
    CapabilityName,
    CapabilitySet,
    HealthState,
    SourceHealth,
    VerificationLevel,
)
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.mail import (
    AttachmentBytes,
    AttachmentMeta,
    Folder,
    FolderRole,
    MailQuery,
    Message,
    MessageSummary,
    bare,
)
from chamberlain.ports.clock import Clock
from chamberlain.ports.page import Gap, Page
from chamberlain.ports.sync import FOLDER_SECONDS, FolderOutcome, SyncReport, run_sync
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_K = IntentKind
_ABSENCE_KINDS = {
    CapabilityName.MAIL_DRAFT: frozenset(
        {_K.MAIL_DRAFT_SAVE.value, _K.MAIL_DRAFT_UPDATE.value, _K.MAIL_DRAFT_DISCARD.value}
    ),
    CapabilityName.MAIL_MOVE: frozenset({_K.MAIL_MOVE.value}),
    CapabilityName.MAIL_TRASH: frozenset({_K.MAIL_TRASH.value}),
    CapabilityName.MAIL_MARK_READ: frozenset({_K.MAIL_MARK_READ.value}),
}
HEALTH_SAMPLE_DAYS: Final = 7
"""The freshness samples look at the Inbox's last week only (a bounded, complete read)."""
_ROLE_BY_USE: Final = {
    "inbox": FolderRole.INBOX,
    "sent": FolderRole.SENT,
    "drafts": FolderRole.DRAFTS,
    "trash": FolderRole.TRASH,
    "junk": FolderRole.JUNK,
    "archive": FolderRole.ARCHIVE,
    "archives": FolderRole.ARCHIVE,
}


class _AccountFields(TypedDict):
    account_id: str
    mailbox: str


class ThunderbirdBinding(Model):
    """One mailbox bound to its Thunderbird account and its exact folders."""

    kind: Literal["thunderbird"] = "thunderbird"
    account_id: str = Field(min_length=1)
    mailbox: str = Field(min_length=3, pattern=r"^[^@\s]+@[^@\s]+$")
    inbox_folder_id: str = Field(min_length=1)
    sent_folder_id: str = Field(min_length=1)
    draft_folder_id: str = Field(min_length=1)
    trash_folder_id: str = Field(min_length=1)

    @property
    def account(self) -> _AccountFields:
        return {"account_id": self.account_id, "mailbox": self.mailbox}


@dataclass(frozen=True)
class MailLimits:
    max_attachment_bytes: int = 25 * 1024 * 1024
    max_recipients: int = 100
    page_size: int = 200
    freshness_ttl_seconds: float = 900.0
    sample_interval_seconds: float = 1.0
    duplicate_lookback_days: int = 30
    reconcile_window_hours: int = 24


class _Cursor(Model):
    q: str
    d: int
    k: str


def _query_hash(query: MailQuery) -> str:
    return sha256_text(query.model_dump_json(exclude={"limit"}))[:16]


def _encode_cursor(cursor: _Cursor) -> str:
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode("ascii").rstrip("=")


def _decode_cursor(text: str, query: MailQuery) -> _Cursor:
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        cursor = _Cursor.model_validate_json(raw)
    except ValueError:
        raise ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid.") from None
    if cursor.q != _query_hash(query):
        raise ChamberlainError(
            ErrorCode.REQUEST_BAD_CURSOR, "That cursor belongs to a different search."
        )
    return cursor


def _first_id(raw: str) -> str:
    parts = raw.split()
    return bracketed(parts[0]) if parts else ""


class MailReads:
    """Read-side operations shared by the Thunderbird mail write shapes."""

    def __init__(
        self,
        client: BridgeClient,
        bindings: Mapping[str, ThunderbirdBinding],
        clock: Clock,
        *,
        limits: MailLimits | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self._bindings = dict(bindings)
        self.clock = clock
        self.limits = limits or MailLimits()
        self._sleep = sleep
        self._fresh: dict[str, datetime] = {}

    # --- description ------------------------------------------------------------------

    def binding(self, source: str) -> ThunderbirdBinding:
        try:
            return self._bindings[source]
        except KeyError:
            raise ChamberlainError(
                ErrorCode.SOURCE_NOT_CONFIGURED, "That mail source is not bound to Thunderbird."
            ) from None

    def capabilities(self, source: str) -> CapabilitySet:
        self.binding(source)
        limits = {
            "max_attachment_bytes": self.limits.max_attachment_bytes,
            "max_recipients": self.limits.max_recipients,
            "page_size": self.limits.page_size,
        }
        return CapabilitySet(
            source=source,
            capabilities=tuple(
                Capability(
                    name=name,
                    level=VerificationLevel.IMPLEMENTED,
                    limits=limits
                    | ({"message_id_honoured": 0} if name is CapabilityName.MAIL_SEND else {}),
                    absence_provable=_ABSENCE_KINDS.get(name, frozenset()),
                )
                for name in CapabilityName
                if name.value.startswith("mail.")
            ),
        )

    def health(self, source: str) -> SourceHealth:
        """`ok` only after a verified provider callback and two equal recent Inbox samples (P-5)."""
        binding = self.binding(source)
        checked = self.clock.now()
        state = HealthState.STALE
        try:
            if self._refresh_health_folders(binding):
                first = self._inbox_sample(binding, checked)
                if first.complete:
                    self._sleep(self.limits.sample_interval_seconds)
                    second = self._inbox_sample(binding, checked)
                    if second.complete and _newest(first) == _newest(second):
                        self._fresh[source] = checked + timedelta(
                            seconds=self.limits.freshness_ttl_seconds
                        )
                        state = HealthState.OK
        except BridgeFailure:
            state = HealthState.UNAVAILABLE
        return SourceHealth(
            source=source,
            state=state,
            checked_at=checked,
            fresh_as_of=checked if state is HealthState.OK else None,
        )

    def _inbox_sample(self, binding: ThunderbirdBinding, checked: datetime) -> protocol.QueryResult:
        # Thunderbird's bounded scan must reach recent mail even in a large Inbox.
        args = protocol.QueryArgs(
            **binding.account,
            folder_id=binding.inbox_folder_id,
            limit=1,
            since_ms=milliseconds(checked) - HEALTH_SAMPLE_DAYS * 86_400_000,
        )
        return self.call(Command.QUERY, args, protocol.QueryResult)

    def _refresh_health_folders(self, binding: ThunderbirdBinding) -> bool:
        folders = dict.fromkeys(
            (
                binding.inbox_folder_id,
                binding.sent_folder_id,
                binding.draft_folder_id,
                binding.trash_folder_id,
            )
        )
        return self._refresh_folders(binding, tuple(folders)).provider_callback_verified

    def _refresh_folders(
        self, binding: ThunderbirdBinding, group: Sequence[str]
    ) -> protocol.RefreshResult:
        args = protocol.RefreshArgs(
            **binding.account, folder_ids=tuple(group), timeout_ms=FOLDER_SECONDS * 1000
        )
        return self.call(Command.REFRESH, args, protocol.RefreshResult)

    def sync_folders(
        self,
        source: str,
        folder_ids: Sequence[str],
        time_limit: timedelta,
        retry: Collection[str] = frozenset(),
    ) -> SyncReport:
        """`FolderSync` (P-20): ask Thunderbird to sync each folder and wait for its answer.

        A few folders go in one call, so the add-on answers inside its read deadline even when
        every one of them is stuck. Thunderbird keeps syncing a folder that missed its wait, so
        a folder in `retry` is asked again, and the second ask usually finds it done."""
        binding = self.binding(source)

        def attempt(group: Sequence[str]) -> list[FolderOutcome]:
            result = self._refresh_folders(binding, group)
            now = self.clock.now()
            return [
                FolderOutcome(folder_id=f.folder_id, synced=f.success, code=f.code, at=now)
                for f in result.folders
            ]

        return run_sync(attempt, folder_ids, self.clock, time_limit, retry)

    def _vouched(self, source: str, binding: ThunderbirdBinding) -> bool:
        """Whether the store may be called fresh now; refreshes at most once per TTL."""
        now = self.clock.now()
        if (until := self._fresh.get(source)) is not None and now < until:
            return True
        if not self._refresh_health_folders(binding):
            return False
        self._fresh[source] = now + timedelta(seconds=self.limits.freshness_ttl_seconds)
        return True

    def call[M: Model](
        self,
        command: Command,
        args: Model,
        result: type[M],
        *,
        payloads: Mapping[str, bytes] | None = None,
    ) -> M:
        return typed(self.client.call(command, args, payloads=payloads), result)

    def folders(self, source: str) -> list[Folder]:
        binding = self.binding(source)
        result = self.call(
            Command.FOLDERS, protocol.AccountArgs(**binding.account), protocol.FoldersResult
        )
        bound_roles = {
            binding.inbox_folder_id: FolderRole.INBOX,
            binding.trash_folder_id: FolderRole.TRASH,
            binding.draft_folder_id: FolderRole.DRAFTS,
            binding.sent_folder_id: FolderRole.SENT,
        }
        found: list[Folder] = []
        for item in result.folders:
            if item.path in ("", "/"):
                continue  # the account root holds no mail
            role = FolderRole.OTHER
            for use in item.special_use:
                role = _ROLE_BY_USE.get(use, role)
            found.append(
                Folder(
                    id=item.id, name=item.name, path=item.path, role=bound_roles.get(item.id, role)
                )
            )
        return found

    def _summary(self, source: str, binding: ThunderbirdBinding, header: Header) -> MessageSummary:
        return MessageSummary(
            source=source,
            locator=encode_locator(
                binding.account_id, header.folder_id, header.internet_message_id
            ),
            folder_id=header.folder_id,
            internet_message_id=bracketed(header.internet_message_id),
            subject=header.subject,
            sender=parse_address(header.author),
            date=datetime.fromtimestamp(header.date_ms / 1000, UTC),
            unread=not header.read,
            has_attachments=header.has_attachments,
            to=emails_only(header.recipients),
            cc=emails_only(header.cc),
        )

    def search(
        self, source: str, query: MailQuery, cursor: str | None = None
    ) -> Page[MessageSummary]:
        binding = self.binding(source)
        after = None
        if cursor is not None:
            decoded = _decode_cursor(cursor, query)
            after = protocol.QueryCursor(date_ms=decoded.d, key=decoded.k)
        gaps: list[Gap] = []
        if not self._vouched(source, binding):
            gaps.append(
                Gap(source=source, reason="The mail store was not confirmed to be in sync.")
            )
        # The bridge matches words in headers. Full-text search can exceed its read deadline.
        args = protocol.QueryArgs(
            **binding.account,
            folder_id=query.folder_id,
            internet_message_id=bare(query.internet_message_id)
            if query.internet_message_id
            else None,
            words=query.text,
            unread=query.unread,
            since_ms=_ms_opt(query.since),
            until_ms=_ms_opt(query.until),
            limit=min(query.limit, self.limits.page_size),
            after=after,
        )
        result = self.call(Command.QUERY, args, protocol.QueryResult)
        if not result.complete:
            gaps.append(
                Gap(source=source, reason="The search stopped at its scan limit.", narrow=True)
            )
        next_cursor = None
        if result.more and result.next is not None:
            next_cursor = _encode_cursor(
                _Cursor(q=_query_hash(query), d=result.next.date_ms, k=result.next.key)
            )
        return Page[MessageSummary](
            items=tuple(self._summary(source, binding, header) for header in result.messages),
            cursor=next_cursor,
            complete=next_cursor is None and not gaps,
            gaps=tuple(gaps),
        )

    def _locate(self, source: str, locator: str) -> tuple[ThunderbirdBinding, MessageLocator]:
        binding = self.binding(source)
        return binding, decode_locator(locator, account_id=binding.account_id)

    def copies_of(
        self, binding: ThunderbirdBinding, folder_id: str, message_id: str, *, draft: bool = False
    ) -> protocol.CopyScopeResult:
        scope = CopyScope(folder_id=folder_id, ids=(message_id,), inspect_draft=draft)
        return self.copies(binding, [scope]).scopes[0]

    def resolve_one(self, binding: ThunderbirdBinding, where: MessageLocator) -> Header:
        scope = self.copies_of(binding, where.folder_id, bare(where.message_id))
        copies = scope.copies
        if not scope.complete:
            raise ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, "The folder scan was incomplete.")
        if len(copies) == 1:
            return copies[0].header
        if not copies:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE, "That message is not in Thunderbird."
            )
        raise ChamberlainError(
            ErrorCode.REMOTE_AMBIGUOUS, "More than one copy of that message is in the folder."
        )

    def resolve(self, source: str, locator: str) -> MessageSummary:
        binding, where = self._locate(source, locator)
        return self._summary(source, binding, self.resolve_one(binding, where))

    def folder_of(self, source: str, locator: str) -> str:
        return self._locate(source, locator)[1].folder_id

    def read(self, source: str, locator: str) -> Message:
        binding, where = self._locate(source, locator)
        args = protocol.ReadArgs(
            **binding.account,
            folder_id=where.folder_id,
            internet_message_id=where.message_id,
            raw_hash=True,
        )
        result = self.call(Command.READ, args, protocol.ReadResult)
        summary = self._summary(source, binding, result.header)
        attachments = tuple(
            AttachmentMeta(
                id=item.part_name,
                filename=item.name,
                media_type=item.content_type or "application/octet-stream",
                size=item.size,
            )
            for item in result.attachments
        )
        return Message.model_validate(
            {
                **summary.model_dump(),
                "subject": result.subject,
                "has_attachments": bool(attachments),
                "to": emails_only(result.to),
                "cc": emails_only(result.cc),
                "body_text": result.body,
                "raw_sha256": result.raw_sha256,
                "attachments": attachments,
                "authentication_results": result.authentication_results,
                "in_reply_to": _first_id(result.in_reply_to),
                "references": tuple(bracketed(i) for i in result.references.split() if i),
            }
        )

    def attachment(self, source: str, locator: str, attachment_id: str) -> AttachmentBytes:
        binding, where = self._locate(source, locator)
        reply = self.client.call(
            Command.ATTACHMENT,
            protocol.AttachmentArgs(
                **binding.account,
                folder_id=where.folder_id,
                internet_message_id=where.message_id,
                part_name=attachment_id,
                max_bytes=self.limits.max_attachment_bytes,
            ),
        )
        result = typed(reply, protocol.AttachmentResult)
        data = reply.assembled(attachment_id[:64], expected=result.chunks)
        digest = sha256_bytes(data)
        if len(data) != result.size or digest != result.sha256:
            raise ChamberlainError(
                ErrorCode.ARTIFACT_HASH_MISMATCH, "The attachment bytes did not match their hash."
            )
        return AttachmentBytes(
            meta=AttachmentMeta(
                id=attachment_id,
                filename=result.name,
                size=result.size,
                media_type=result.media_type or "application/octet-stream",
            ),
            data=data,
            sha256=digest,
        )

    def copies(
        self,
        binding: ThunderbirdBinding,
        scopes: Sequence[protocol.CopyScope],
        *,
        refresh: bool = False,
    ) -> protocol.CopiesResult:
        args = protocol.CopiesArgs(**binding.account, where=tuple(scopes), refresh=refresh)
        result = self.call(Command.COPIES, args, protocol.CopiesResult)
        if len(result.scopes) != len(scopes):
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE, "The copy scan returned incomplete coverage."
            )
        return result

    def absence_provable(self, source: str, kind: IntentKind) -> bool:
        self.binding(source)
        return any(kind.value in kinds for kinds in _ABSENCE_KINDS.values())


def _newest(result: protocol.QueryResult) -> tuple[str, int] | None:
    if result.messages:
        header = result.messages[0]
        return header.internet_message_id, header.date_ms
    return None


def milliseconds(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _ms_opt(moment: datetime | None) -> int | None:
    return None if moment is None else milliseconds(moment)
