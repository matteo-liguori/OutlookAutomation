"""Thunderbird mail reads, synchronization and bounded copy observations."""

import base64
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

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
    Header,
    MessageRef,
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
from chamberlain.domain.intents import (
    IntentKind,
)
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


@dataclass(frozen=True)
class ThunderbirdBinding:
    """One mailbox bound to its Thunderbird account and its exact folders."""

    account_id: str
    mailbox: str
    inbox_folder_id: str
    sent_folder_id: str
    draft_folder_id: str
    trash_folder_id: str


@dataclass(frozen=True)
class MailLimits:
    max_attachment_bytes: int = 25 * 1024 * 1024
    max_recipients: int = 100
    page_size: int = 200
    freshness_ttl_seconds: float = 900.0
    sample_interval_seconds: float = 1.0
    duplicate_lookback_days: int = 30
    reconcile_window_hours: int = 24


@dataclass
class _Freshness:
    until: datetime | None = None


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
        self._fresh: dict[str, _Freshness] = {}

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
        provable: dict[CapabilityName, frozenset[str]] = {
            CapabilityName.MAIL_DRAFT: frozenset(
                {_K.MAIL_DRAFT_SAVE.value, _K.MAIL_DRAFT_UPDATE.value, _K.MAIL_DRAFT_DISCARD.value}
            ),
            CapabilityName.MAIL_MOVE: frozenset({_K.MAIL_MOVE.value}),
            CapabilityName.MAIL_TRASH: frozenset({_K.MAIL_TRASH.value}),
            CapabilityName.MAIL_MARK_READ: frozenset({_K.MAIL_MARK_READ.value}),
        }
        names = [n for n in CapabilityName if n.value.startswith("mail.")]
        capabilities: list[Capability] = []
        for name in names:
            extra = {"message_id_honoured": 0} if name is CapabilityName.MAIL_SEND else {}
            capabilities.append(
                Capability(
                    name=name,
                    level=VerificationLevel.IMPLEMENTED,
                    limits={**limits, **extra},
                    absence_provable=provable.get(name, frozenset()),
                )
            )
        return CapabilitySet(source=source, capabilities=tuple(capabilities))

    def health(self, source: str) -> SourceHealth:
        """`ok` only after a verified provider callback and two equal recent Inbox samples (P-5)."""
        binding = self.binding(source)
        checked = self.clock.now()
        try:
            refreshed = self._refresh(binding, self._health_folders(binding))
            if not refreshed:
                return self._state(source, HealthState.STALE, checked)
            samples: list[tuple[str, int] | None] = []
            for attempt in range(2):
                if attempt:
                    self._sleep(self.limits.sample_interval_seconds)
                # Recent mail only: Thunderbird lists a folder oldest first and the add-on stops
                # at `max_scan`, so an unbounded sample of a large Inbox never reaches its
                # newest message and is never complete (found live, 2026-11-25).
                result = self._query(
                    binding,
                    folder_id=binding.inbox_folder_id,
                    limit=1,
                    since_ms=milliseconds(checked) - HEALTH_SAMPLE_DAYS * 86_400_000,
                )
                if not result.complete:
                    return self._state(source, HealthState.STALE, checked)
                newest = result.messages[0] if result.messages else None
                samples.append((newest.internet_message_id, newest.date_ms) if newest else None)
        except BridgeFailure:
            return self._state(source, HealthState.UNAVAILABLE, checked)
        if samples[0] != samples[1]:
            return self._state(source, HealthState.STALE, checked)
        self._fresh[source] = _Freshness(
            until=checked + _seconds(self.limits.freshness_ttl_seconds)
        )
        return self._state(source, HealthState.OK, checked, fresh_as_of=checked)

    @staticmethod
    def _state(
        source: str, state: HealthState, checked: datetime, fresh_as_of: datetime | None = None
    ) -> SourceHealth:
        return SourceHealth.model_validate(
            {
                "source": source,
                "state": state,
                "checked_at": checked,
                "fresh_as_of": fresh_as_of,
            }
        )

    @staticmethod
    def _health_folders(binding: ThunderbirdBinding) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    binding.inbox_folder_id,
                    binding.sent_folder_id,
                    binding.draft_folder_id,
                    binding.trash_folder_id,
                )
            )
        )

    def _refresh(self, binding: ThunderbirdBinding, folder_ids: tuple[str, ...]) -> bool:
        return self._refresh_folders(binding, folder_ids).provider_callback_verified

    def _refresh_folders(
        self, binding: ThunderbirdBinding, group: Sequence[str]
    ) -> protocol.RefreshResult:
        return typed(
            self.client.call(
                Command.REFRESH,
                protocol.RefreshArgs(
                    account_id=binding.account_id,
                    mailbox=binding.mailbox,
                    folder_ids=tuple(group),
                    timeout_ms=FOLDER_SECONDS * 1000,
                ),
            ),
            protocol.RefreshResult,
        )

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
        state = self._fresh.get(source)
        now = self.clock.now()
        if state is not None and state.until is not None and now < state.until:
            return True
        if not self._refresh(binding, self._health_folders(binding)):
            return False
        self._fresh[source] = _Freshness(until=now + _seconds(self.limits.freshness_ttl_seconds))
        return True

    # --- reading ------------------------------------------------------------------------

    def _query(
        self,
        binding: ThunderbirdBinding,
        *,
        folder_id: str | None = None,
        internet_message_id: str | None = None,
        subject: str | None = None,
        unread: bool | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 25,
        after: protocol.QueryCursor | None = None,
        words: str | None = None,
        max_scan: int = 2000,
    ) -> protocol.QueryResult:
        args = protocol.QueryArgs(
            account_id=binding.account_id,
            mailbox=binding.mailbox,
            folder_id=folder_id,
            internet_message_id=bare(internet_message_id) if internet_message_id else None,
            subject=subject,
            words=words,
            unread=unread,
            since_ms=since_ms,
            until_ms=until_ms,
            limit=limit,
            after=after,
            max_scan=max_scan,
        )
        return typed(self.client.call(Command.QUERY, args), protocol.QueryResult)

    def folders(self, source: str) -> list[Folder]:
        binding = self.binding(source)
        reply = self.client.call(
            Command.FOLDERS,
            protocol.AccountArgs(account_id=binding.account_id, mailbox=binding.mailbox),
        )
        found: list[Folder] = []
        for item in typed(reply, protocol.FoldersResult).folders:
            if item.path in ("", "/"):
                continue  # the account's root folder holds no mail and cannot be synced
            role = FolderRole.OTHER
            for use in item.special_use:
                role = _ROLE_BY_USE.get(use, role)
            if item.id == binding.sent_folder_id:
                role = FolderRole.SENT
            elif item.id == binding.draft_folder_id:
                role = FolderRole.DRAFTS
            elif item.id == binding.trash_folder_id:
                role = FolderRole.TRASH
            elif item.id == binding.inbox_folder_id:
                role = FolderRole.INBOX
            found.append(Folder(id=item.id, name=item.name, path=item.path, role=role))
        return found

    def _summary(self, source: str, binding: ThunderbirdBinding, header: Header) -> MessageSummary:
        return MessageSummary.model_validate(
            {
                "source": source,
                "locator": encode_locator(
                    binding.account_id, header.folder_id, header.internet_message_id
                ),
                "folder_id": header.folder_id,
                "internet_message_id": bracketed(header.internet_message_id),
                "subject": header.subject,
                "sender": parse_address(header.author),
                "date": datetime.fromtimestamp(header.date_ms / 1000, UTC),
                "unread": not header.read,
                "has_attachments": header.has_attachments,
                "preview": "",
                "to": emails_only(header.recipients),
                "cc": emails_only(header.cc),
            }
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
                Gap.model_validate(
                    {"source": source, "reason": "The mail store was not confirmed to be in sync."}
                )
            )
        # Words match the subject, sender and recipients. Thunderbird's full-text search did not
        # finish within the bridge's deadline on the owner's real mailboxes, even over three days
        # (found live, 2026-11-25), while a header match over 90 days takes about two seconds.
        result = self._query(
            binding,
            folder_id=query.folder_id,
            internet_message_id=query.internet_message_id,
            words=query.text,
            unread=query.unread,
            since_ms=_ms_opt(query.since),
            until_ms=_ms_opt(query.until),
            limit=min(query.limit, self.limits.page_size),
            after=after,
        )
        if not result.complete:
            gaps.append(
                Gap.model_validate(
                    {
                        "source": source,
                        "reason": "The search stopped at its scan limit.",
                        "narrow": True,
                    }
                )
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

    def resolve_one(
        self, binding: ThunderbirdBinding, where: MessageLocator | MessageRef
    ) -> Header:
        if isinstance(where, MessageLocator):
            folder_id, message_id = where.folder_id, where.message_id
        else:
            folder_id, message_id = where.folder_id, where.internet_message_id
        result = self.copies(
            binding, [protocol.CopyScope(folder_id=folder_id, ids=(bare(message_id),))]
        )
        scope = result.scopes[0]
        if not scope.complete:
            raise ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, "The folder scan was incomplete.")
        found = [copy.header for copy in scope.copies]
        if not found:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE, "That message is not in Thunderbird."
            )
        if len(found) > 1:
            raise ChamberlainError(
                ErrorCode.REMOTE_AMBIGUOUS, "More than one copy of that message is in the folder."
            )
        return found[0]

    def resolve(self, source: str, locator: str) -> MessageSummary:
        binding, where = self._locate(source, locator)
        return self._summary(source, binding, self.resolve_one(binding, where))

    def folder_of(self, source: str, locator: str) -> str:
        return self._locate(source, locator)[1].folder_id

    def read(self, source: str, locator: str) -> Message:
        binding, where = self._locate(source, locator)
        reply = self.client.call(
            Command.READ,
            protocol.ReadArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                folder_id=where.folder_id,
                internet_message_id=where.message_id,
                raw_hash=True,
            ),
        )
        result = typed(reply, protocol.ReadResult)
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
                account_id=binding.account_id,
                mailbox=binding.mailbox,
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
        reply = self.client.call(
            Command.COPIES,
            protocol.CopiesArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                where=tuple(scopes),
                refresh=refresh,
            ),
        )
        result = typed(reply, protocol.CopiesResult)
        if len(result.scopes) != len(scopes):
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE, "The copy scan returned incomplete coverage."
            )
        return result

    def absence_provable(self, source: str, kind: IntentKind) -> bool:
        for capability in self.capabilities(source).capabilities:
            if kind.value in capability.absence_provable:
                return True
        return False


def _seconds(value: float) -> timedelta:
    return timedelta(seconds=value)


def milliseconds(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _ms_opt(moment: datetime | None) -> int | None:
    return None if moment is None else milliseconds(moment)
