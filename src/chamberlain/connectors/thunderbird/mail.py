"""MailPort over the Thunderbird bridge.

Contract: docs/contracts/08-ports.md and docs/contracts/12-bridge-v2.md. The
connector does only what it is told and reports exactly what happened. It
checks no grants, writes no store and keeps no journal (P-13): `prepare` is
read-only, `dispatch` issues one command and never retries, `reconcile` reads.
Behaviours carried over from the old adapter are named in the tests.
"""

import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, TypeVar

from pydantic import JsonValue

from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.client import BridgeClient, BridgeFailure, Reply
from chamberlain.connectors.thunderbird.frames import CHUNK_BYTES
from chamberlain.connectors.thunderbird.identity import (
    MessageLocator,
    bare,
    bracketed,
    decode_locator,
    emails_only,
    encode_locator,
    normalise_body,
    parse_address,
)
from chamberlain.connectors.thunderbird.protocol import (
    AttachmentSpec,
    Command,
    Fingerprint,
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
from chamberlain.ports.clock import Clock
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_K = IntentKind
_M = TypeVar("_M", bound=Model)

ALLOW_DUPLICATE_REF: Final = "_allow_duplicate"
"""Reserved key in `refs`: the engine sets it to "1" when the grant allows a duplicate send (R3)."""
DISPLAY_ONLY_REF: Final = "_display_only"
"""Reserved key in `refs`: leave a saved draft's composer open (R8) until the intent has a field."""

_PRE_MUTATION_ITEM_CODES: Final = frozenset(
    {"not_found", "ambiguous", "account_mismatch", "timeout", "busy"}
)
_ROLE_BY_USE: Final = {
    "inbox": FolderRole.INBOX,
    "sent": FolderRole.SENT,
    "drafts": FolderRole.DRAFTS,
    "trash": FolderRole.TRASH,
    "archive": FolderRole.ARCHIVE,
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


# --- typed plans stored in `Prepared.data` -----------------------------------------------------


class PlanAttachment(Model):
    sha256: str
    filename: str


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


class MovePlan(Model):
    ref: MessageRef
    destination_folder_id: str
    mark_read: bool
    source_before: int
    destination_before: int
    was_unread: bool


class MarkPlan(Model):
    refs: tuple[MessageRef, ...]
    read: bool


class DiscardPlan(Model):
    draft: MessageRef
    expected_raw_sha256: str


def _plan[M: Model](prepared: Prepared, model: type[M]) -> M:
    try:
        return model.model_validate(prepared.data)
    except ValueError:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "The prepared write is not one this connector made."
        ) from None


def _typed[M: Model](reply: Reply, model: type[M]) -> M:
    if not isinstance(reply.result, model):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "Unexpected bridge result type.")
    return reply.result


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


class ThunderbirdMail:
    """Implements `chamberlain.ports.MailPort` for bound Thunderbird mailboxes."""

    def __init__(
        self,
        client: BridgeClient,
        bindings: Mapping[str, ThunderbirdBinding],
        clock: Clock,
        *,
        limits: MailLimits | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._bindings = dict(bindings)
        self._clock = clock
        self._limits = limits or MailLimits()
        self._sleep = sleep
        self._fresh: dict[str, _Freshness] = {}

    # --- description ------------------------------------------------------------------

    def _binding(self, source: str) -> ThunderbirdBinding:
        try:
            return self._bindings[source]
        except KeyError:
            raise ChamberlainError(
                ErrorCode.SOURCE_NOT_CONFIGURED, "That mail source is not bound to Thunderbird."
            ) from None

    def capabilities(self, source: str) -> CapabilitySet:
        self._binding(source)
        limits = {
            "max_attachment_bytes": self._limits.max_attachment_bytes,
            "max_recipients": self._limits.max_recipients,
            "page_size": self._limits.page_size,
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
        binding = self._binding(source)
        checked = self._clock.now()
        try:
            refreshed = self._refresh(binding, self._health_folders(binding))
            if not refreshed:
                return self._state(source, HealthState.STALE, checked)
            samples: list[tuple[str, int] | None] = []
            for attempt in range(2):
                if attempt:
                    self._sleep(self._limits.sample_interval_seconds)
                result = self._query(binding, folder_id=binding.inbox_folder_id, limit=1)
                if not result.complete:
                    return self._state(source, HealthState.STALE, checked)
                newest = result.messages[0] if result.messages else None
                samples.append((newest.internet_message_id, newest.date_ms) if newest else None)
        except BridgeFailure as error:
            if error.bridge_code is protocol.BridgeErrorCode.OFFLINE:
                return self._state(source, HealthState.UNAVAILABLE, checked)
            return self._state(source, HealthState.UNAVAILABLE, checked)
        if samples[0] != samples[1]:
            return self._state(source, HealthState.STALE, checked)
        self._fresh[source] = _Freshness(
            until=checked + _seconds(self._limits.freshness_ttl_seconds)
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
        reply = self._client.call(
            Command.REFRESH,
            protocol.RefreshArgs(
                account_id=binding.account_id, mailbox=binding.mailbox, folder_ids=folder_ids
            ),
        )
        return _typed(reply, protocol.RefreshResult).provider_callback_verified

    def _vouched(self, source: str, binding: ThunderbirdBinding) -> bool:
        """Whether the store may be called fresh now; refreshes at most once per TTL."""
        state = self._fresh.get(source)
        now = self._clock.now()
        if state is not None and state.until is not None and now < state.until:
            return True
        if not self._refresh(binding, self._health_folders(binding)):
            return False
        self._fresh[source] = _Freshness(until=now + _seconds(self._limits.freshness_ttl_seconds))
        return True

    # --- reading ------------------------------------------------------------------------

    def _query(
        self,
        binding: ThunderbirdBinding,
        *,
        folder_id: str | None = None,
        internet_message_id: str | None = None,
        text: str | None = None,
        subject: str | None = None,
        unread: bool | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 25,
        after: protocol.QueryCursor | None = None,
        own_only: bool = False,
        subject_contains: bool = False,
        max_scan: int = 2000,
    ) -> protocol.QueryResult:
        args = protocol.QueryArgs(
            account_id=binding.account_id,
            mailbox=binding.mailbox,
            folder_id=folder_id,
            internet_message_id=bare(internet_message_id) if internet_message_id else None,
            text=text,
            subject=subject,
            subject_contains=subject_contains,
            unread=unread,
            since_ms=since_ms,
            until_ms=until_ms,
            limit=limit,
            after=after,
            max_scan=max_scan,
            own_only=own_only,
        )
        return _typed(self._client.call(Command.QUERY, args), protocol.QueryResult)

    def folders(self, source: str) -> list[Folder]:
        binding = self._binding(source)
        reply = self._client.call(
            Command.FOLDERS,
            protocol.FoldersArgs(account_id=binding.account_id, mailbox=binding.mailbox),
        )
        found: list[Folder] = []
        for item in _typed(reply, protocol.FoldersResult).folders:
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
        binding = self._binding(source)
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
        result = self._query(
            binding,
            folder_id=query.folder_id,
            internet_message_id=query.internet_message_id,
            text=query.text,
            unread=query.unread,
            since_ms=_ms_opt(query.since),
            until_ms=_ms_opt(query.until),
            limit=min(query.limit, self._limits.page_size),
            after=after,
        )
        if not result.complete:
            gaps.append(
                Gap.model_validate(
                    {"source": source, "reason": "The search stopped at its scan limit."}
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
        binding = self._binding(source)
        return binding, decode_locator(locator, account_id=binding.account_id)

    def _resolve_one(
        self, binding: ThunderbirdBinding, where: MessageLocator | MessageRef
    ) -> Header:
        if isinstance(where, MessageLocator):
            folder_id, message_id = where.folder_id, where.message_id
        else:
            folder_id, message_id = where.folder_id, where.internet_message_id
        reply = self._client.call(
            Command.RESOLVE,
            protocol.ResolveArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                folder_id=folder_id,
                internet_message_id=bare(message_id),
            ),
        )
        result = _typed(reply, protocol.ResolveResult)
        if not result.complete:
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE, "The folder could not be scanned completely."
            )
        found = list(result.messages)
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
        return self._summary(source, binding, self._resolve_one(binding, where))

    def read(self, source: str, locator: str) -> Message:
        binding, where = self._locate(source, locator)
        reply = self._client.call(
            Command.READ,
            protocol.ReadArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                folder_id=where.folder_id,
                internet_message_id=where.message_id,
                raw_hash=True,
            ),
        )
        result = _typed(reply, protocol.ReadResult)
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
        message = self.read(source, locator)
        meta = next((item for item in message.attachments if item.id == attachment_id), None)
        if meta is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such attachment.")
        if meta.size > self._limits.max_attachment_bytes:
            raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "That attachment is too large.")
        reply = self._client.call(
            Command.ATTACHMENT,
            protocol.AttachmentArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                folder_id=where.folder_id,
                internet_message_id=where.message_id,
                part_name=attachment_id,
                max_bytes=self._limits.max_attachment_bytes,
            ),
        )
        result = _typed(reply, protocol.AttachmentResult)
        data = reply.assembled(attachment_id[:64], expected=result.chunks)
        digest = sha256_bytes(data)
        if len(data) != result.size or digest != result.sha256:
            raise ChamberlainError(
                ErrorCode.ARTIFACT_HASH_MISMATCH, "The attachment bytes did not match their hash."
            )
        return AttachmentBytes(meta=meta, data=data, sha256=digest)

    # --- prepare ------------------------------------------------------------------------

    def prepare(self, intent: MailIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only (P-9): resolve, check preconditions, hold everything dispatch needs."""
        binding = self._binding(intent.source)
        if isinstance(intent, MailSend | MailDraftSave | MailDraftUpdate):
            return self._prepare_compose(binding, intent, refs)
        if isinstance(intent, MailDraftDiscard):
            draft = self._draft_ref(binding, refs, intent.draft, intent.expected_sha256)
            plan = DiscardPlan(draft=draft, expected_raw_sha256=intent.expected_sha256)
            return self._prepared(intent.source, intent.kind, None, plan)
        if isinstance(intent, MailMove | MailTrash):
            return self._prepare_move(binding, intent, refs)
        return self._prepare_mark_read(binding, intent, refs)

    @staticmethod
    def _prepared(source: str, kind: IntentKind, token: str | None, plan: Model) -> Prepared:
        data: dict[str, JsonValue] = plan.model_dump(mode="json")
        return Prepared.model_validate(
            {"source": source, "intent_kind": kind, "reconcile_token": token, "data": data}
        )

    def _ref_of(
        self, binding: ThunderbirdBinding, refs: Mapping[str, str], handle: str
    ) -> MessageLocator:
        locator = refs.get(handle)
        if locator is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE, "A handle in the request has no locator."
            )
        return decode_locator(locator, account_id=binding.account_id)

    def _draft_ref(
        self,
        binding: ThunderbirdBinding,
        refs: Mapping[str, str],
        handle: str,
        expected_sha256: str,
    ) -> MessageRef:
        where = self._ref_of(binding, refs, handle)
        if where.folder_id != binding.draft_folder_id:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "That message is not a draft.")
        header = self._resolve_one(binding, where)
        raw = self._raw_hash(binding, where)
        if raw != expected_sha256:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The draft changed since it was read.")
        return MessageRef(folder_id=header.folder_id, internet_message_id=where.message_id)

    def _raw_hash(self, binding: ThunderbirdBinding, where: MessageLocator) -> str | None:
        reply = self._client.call(
            Command.READ,
            protocol.ReadArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                folder_id=where.folder_id,
                internet_message_id=where.message_id,
                raw_hash=True,
            ),
        )
        return _typed(reply, protocol.ReadResult).raw_sha256

    def _prepare_compose(
        self,
        binding: ThunderbirdBinding,
        intent: MailSend | MailDraftSave | MailDraftUpdate,
        refs: Mapping[str, str],
    ) -> Prepared:
        if intent.message_id is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "The message has no Message-ID.", field="message_id"
            )
        recipients = len(intent.to) + len(intent.cc) + len(intent.bcc)
        if recipients > self._limits.max_recipients:
            raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "Too many recipients.")
        reply_to = None
        if intent.reply_to is not None:
            where = self._ref_of(binding, refs, intent.reply_to)
            self._resolve_one(binding, where)
            reply_to = MessageRef(folder_id=where.folder_id, internet_message_id=where.message_id)
        draft = None
        expected = None
        if isinstance(intent, MailDraftUpdate):
            draft = self._draft_ref(binding, refs, intent.draft, intent.expected_sha256)
            expected = intent.expected_sha256
        own = binding.mailbox.lower()
        everyone = {*intent.to, *intent.cc, *intent.bcc}
        fingerprint = Fingerprint(
            sender=own,
            to=tuple(sorted(a.lower() for a in intent.to)),
            cc=tuple(sorted(a.lower() for a in intent.cc)),
            bcc=tuple(sorted(a.lower() for a in intent.bcc)),
            subject=intent.subject,
            body_sha256=sha256_text(normalise_body(intent.body)),
            attachment_sha256=tuple(sorted(a.sha256 for a in intent.attachments)),
            high_importance=intent.high_importance,
            in_reply_to=bracketed(reply_to.internet_message_id) if reply_to else "",
            token=intent.message_id,
        )
        plan = ComposePlan(
            to=intent.to,
            cc=intent.cc,
            bcc=intent.bcc,
            subject=intent.subject,
            body=intent.body,
            high_importance=intent.high_importance,
            token=intent.message_id,
            sent_folder_id=binding.sent_folder_id,
            draft_folder_id=binding.draft_folder_id,
            reply_to=reply_to,
            display_only=refs.get(DISPLAY_ONLY_REF) == "1",
            attachments=tuple(
                PlanAttachment(sha256=a.sha256, filename=a.filename) for a in intent.attachments
            ),
            fingerprint=fingerprint,
            since_ms=_ms(self._clock.now()) - self._limits.reconcile_window_hours * 3_600_000,
            inbox_folder_id=binding.inbox_folder_id if everyone == {own} else None,
            draft=draft,
            expected_raw_sha256=expected,
        )
        if isinstance(intent, MailSend) and refs.get(ALLOW_DUPLICATE_REF) != "1":
            self._duplicate_scan(binding, plan)
        return self._prepared(intent.source, intent.kind, intent.message_id, plan)

    def _duplicate_scan(self, binding: ThunderbirdBinding, plan: ComposePlan) -> None:
        """R3: no related message may already sit in Sent, Drafts or Outbox."""
        listing = self._client.call(
            Command.FOLDERS,
            protocol.FoldersArgs(
                account_id=binding.account_id, mailbox=binding.mailbox, include_outbox=True
            ),
        )
        outboxes = [
            f.id
            for f in _typed(listing, protocol.FoldersResult).folders
            if "outbox" in f.special_use
        ]
        since = _ms(self._clock.now()) - self._limits.duplicate_lookback_days * 86_400_000
        blocked = ChamberlainError(
            ErrorCode.REMOTE_CHANGED,
            "A related message is already in Sent, Drafts or Outbox, or they could not be checked.",
            fix="Check Sent and Drafts. Send again only if the owner allows a duplicate.",
        )
        wanted = {a.lower() for a in (*plan.to, *plan.cc, *plan.bcc)}
        for folder_id in dict.fromkeys((plan.sent_folder_id, plan.draft_folder_id, *outboxes)):
            found = self._query(
                binding,
                folder_id=folder_id,
                subject=plan.subject,
                subject_contains=True,
                since_ms=since,
                own_only=True,
                limit=200,
                max_scan=500,
            )
            if not found.complete or found.more:
                raise blocked
            for header in found.messages:
                read = _typed(
                    self._client.call(
                        Command.READ,
                        protocol.ReadArgs(
                            account_id=binding.account_id,
                            mailbox=binding.mailbox,
                            folder_id=header.folder_id,
                            internet_message_id=header.internet_message_id,
                        ),
                    ),
                    protocol.ReadResult,
                )
                same_body = sha256_text(normalise_body(read.body)) == plan.fingerprint.body_sha256
                overlap = wanted & {a.lower() for a in (*read.to, *read.cc, *read.bcc)}
                if same_body or (not normalise_body(plan.body) and overlap):
                    raise blocked

    def _prepare_move(
        self, binding: ThunderbirdBinding, intent: MailMove | MailTrash, refs: Mapping[str, str]
    ) -> Prepared:
        where = self._ref_of(binding, refs, intent.message)
        header = self._resolve_one(binding, where)
        if intent.internet_message_id is not None and bare(intent.internet_message_id) != bare(
            header.internet_message_id
        ):
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The message identity differs.")
        if intent.from_folder is not None and intent.from_folder != where.folder_id:
            raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "The message is no longer there.")
        if isinstance(intent, MailMove):
            destination = intent.to_folder
            mark_read = intent.mark_read
        else:
            destination = binding.trash_folder_id
            mark_read = False
        if destination == where.folder_id:
            raise ChamberlainError(
                ErrorCode.REMOTE_CHANGED, "The message is already in that folder."
            )
        known = {f.id for f in self.folders(intent.source)}
        if destination not in known:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "That destination folder does not exist.",
                field="to_folder",
            )
        there = _typed(
            self._client.call(
                Command.RESOLVE,
                protocol.ResolveArgs(
                    account_id=binding.account_id,
                    mailbox=binding.mailbox,
                    folder_id=destination,
                    internet_message_id=where.message_id,
                ),
            ),
            protocol.ResolveResult,
        )
        if isinstance(intent, MailMove) and there.messages:
            raise ChamberlainError(
                ErrorCode.REMOTE_AMBIGUOUS, "The destination already holds this message."
            )
        plan = MovePlan(
            ref=MessageRef(folder_id=where.folder_id, internet_message_id=where.message_id),
            destination_folder_id=destination,
            mark_read=mark_read,
            source_before=1,
            destination_before=len(there.messages),
            was_unread=not header.read,
        )
        return self._prepared(intent.source, intent.kind, None, plan)

    def _prepare_mark_read(
        self, binding: ThunderbirdBinding, intent: MailMarkRead, refs: Mapping[str, str]
    ) -> Prepared:
        located: list[MessageRef] = []
        for handle in intent.messages:
            where = self._ref_of(binding, refs, handle)
            self._resolve_one(binding, where)
            located.append(
                MessageRef(folder_id=where.folder_id, internet_message_id=where.message_id)
            )
        plan = MarkPlan(refs=tuple(located), read=intent.read)
        return self._prepared(intent.source, intent.kind, None, plan)

    # --- dispatch -----------------------------------------------------------------------

    def dispatch(
        self, prepared: Prepared, key: str, payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        """The one mutation (P-10). Never retries, and returns `unknown` rather than raising."""
        binding = self._binding(prepared.source)
        kind = prepared.intent_kind
        try:
            if kind in {_K.MAIL_SEND, _K.MAIL_DRAFT_SAVE, _K.MAIL_DRAFT_UPDATE}:
                return self._dispatch_compose(binding, prepared, payloads)
            if kind is _K.MAIL_DRAFT_DISCARD:
                return self._dispatch_discard(binding, prepared)
            if kind in {_K.MAIL_MOVE, _K.MAIL_TRASH}:
                return self._dispatch_move(binding, prepared)
            return self._dispatch_mark_read(binding, prepared)
        except BridgeFailure as error:
            if error.proven_not_applied:
                return DispatchResult(
                    outcome=DispatchOutcome.NOT_APPLIED,
                    detail=f"{error.code.value}: {error.detail or error.message}"[:300],
                )
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN,
                detail=f"{error.code.value}: {error.detail or error.message}"[:300],
            )
        except ChamberlainError as error:
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail=f"{error.code.value}: {error.detail}"[:300]
            )

    def _check_payloads(
        self, plan: ComposePlan, payloads: Mapping[str, bytes]
    ) -> tuple[list[AttachmentSpec], dict[str, bytes]] | DispatchResult:
        """W-3: hashes are checked here, before any byte leaves; a mismatch is `not_applied`."""
        specs: list[AttachmentSpec] = []
        files: dict[str, bytes] = {}
        for index, item in enumerate(plan.attachments, start=1):
            data = payloads.get(item.sha256)
            if data is None or sha256_bytes(data) != item.sha256:
                return DispatchResult(
                    outcome=DispatchOutcome.NOT_APPLIED,
                    detail="artifact.hash_mismatch: an attachment does not match its hash",
                )
            if len(data) > self._limits.max_attachment_bytes:
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

    def _dispatch_compose(
        self, binding: ThunderbirdBinding, prepared: Prepared, payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        plan = _plan(prepared, ComposePlan)
        checked = self._check_payloads(plan, payloads)
        if isinstance(checked, DispatchResult):
            return checked
        specs, files = checked
        common: dict[str, object] = {
            "account_id": binding.account_id,
            "mailbox": binding.mailbox,
            "to": plan.to,
            "cc": plan.cc,
            "bcc": plan.bcc,
            "subject": plan.subject,
            "body": plan.body,
            "high_importance": plan.high_importance,
            "sent_folder_id": plan.sent_folder_id,
            "draft_folder_id": plan.draft_folder_id,
            "token": plan.token,
            "attachments": tuple(specs),
            "reply_to": plan.reply_to,
            "display_only": plan.display_only,
        }
        kind = prepared.intent_kind
        if kind is _K.MAIL_SEND:
            reply = self._client.call(
                Command.SEND, protocol.SendArgs.model_validate(common), payloads=files
            )
            _typed(reply, protocol.SendResult)
            return DispatchResult(
                outcome=DispatchOutcome.APPLIED,
                receipt=ProviderReceipt(verification=Verification(client_verified=True)),
                detail="submitted to Thunderbird",
            )
        if kind is _K.MAIL_DRAFT_SAVE:
            reply = self._client.call(
                Command.DRAFT_SAVE, protocol.DraftSaveArgs.model_validate(common), payloads=files
            )
        else:
            if plan.draft is None or plan.expected_raw_sha256 is None:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "The draft update lost its draft."
                )
            reply = self._client.call(
                Command.DRAFT_UPDATE,
                protocol.DraftUpdateArgs.model_validate(
                    {**common, "draft": plan.draft, "expected_raw_sha256": plan.expected_raw_sha256}
                ),
                payloads=files,
            )
        result = _typed(reply, protocol.DraftResult)
        locator = encode_locator(
            binding.account_id, result.header.folder_id, result.header.internet_message_id
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
            ),
            detail=note,
        )

    def _dispatch_discard(self, binding: ThunderbirdBinding, prepared: Prepared) -> DispatchResult:
        plan = _plan(prepared, DiscardPlan)
        reply = self._client.call(
            Command.DRAFT_DISCARD,
            protocol.DraftDiscardArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                draft=plan.draft,
                expected_raw_sha256=plan.expected_raw_sha256,
            ),
        )
        result = _typed(reply, protocol.DiscardResult)
        return DispatchResult(
            outcome=DispatchOutcome.APPLIED,
            receipt=ProviderReceipt(verification=Verification(client_verified=True)),
            detail=f"closed {result.closed} composer(s); saved mail untouched",
        )

    def _dispatch_move(self, binding: ThunderbirdBinding, prepared: Prepared) -> DispatchResult:
        plan = _plan(prepared, MovePlan)
        trash = prepared.intent_kind is _K.MAIL_TRASH
        args: dict[str, object] = {
            "account_id": binding.account_id,
            "mailbox": binding.mailbox,
            "ref": plan.ref,
            "destination_folder_id": plan.destination_folder_id,
        }
        if trash:
            reply = self._client.call(Command.TRASH, protocol.TrashArgs.model_validate(args))
        else:
            reply = self._client.call(
                Command.MOVE,
                protocol.MoveArgs.model_validate({**args, "mark_read": plan.mark_read}),
            )
        result = _typed(reply, protocol.MoveResult)
        if result.message.folder_id != plan.destination_folder_id:
            return DispatchResult(
                outcome=DispatchOutcome.UNKNOWN, detail="the moved copy is not in the destination"
            )
        warnings = ("merged_existing_copy",) if result.merged_existing_copy else ()
        return DispatchResult(
            outcome=DispatchOutcome.APPLIED,
            receipt=ProviderReceipt(
                locator=encode_locator(
                    binding.account_id,
                    result.message.folder_id,
                    result.message.internet_message_id,
                ),
                verification=Verification(client_verified=True),
                read_state_preserved=result.read_state_preserved,
                warnings=warnings,
            ),
            detail=warnings[0] if warnings else "",
        )

    def _dispatch_mark_read(
        self, binding: ThunderbirdBinding, prepared: Prepared
    ) -> DispatchResult:
        plan = _plan(prepared, MarkPlan)
        reply = self._client.call(
            Command.MARK_READ,
            protocol.MarkReadArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                refs=plan.refs,
                read=plan.read,
            ),
        )
        items = _typed(reply, protocol.MarkReadResult).items
        results: list[ItemResult] = []
        for item in items:
            locator = encode_locator(
                binding.account_id, item.ref.folder_id, item.ref.internet_message_id
            )
            code: ErrorCode | None = None
            if not item.ok:
                code = {
                    "not_found": ErrorCode.REQUEST_UNKNOWN_HANDLE,
                    "ambiguous": ErrorCode.REMOTE_AMBIGUOUS,
                }.get(item.code, ErrorCode.SOURCE_UNAVAILABLE)
            results.append(ItemResult(locator=locator, ok=item.ok, code=code))
        if any(item.ok for item in items):
            return DispatchResult(
                outcome=DispatchOutcome.APPLIED,
                receipt=ProviderReceipt(
                    verification=Verification(client_verified=True), items=tuple(results)
                ),
            )
        if all(item.code in _PRE_MUTATION_ITEM_CODES for item in items):
            return DispatchResult(
                outcome=DispatchOutcome.NOT_APPLIED, detail="no message could be marked"
            )
        return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="marking was not verified")

    # --- reconcile ----------------------------------------------------------------------

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11): needs only `prepared`; may refresh exactly the folders involved."""
        binding = self._binding(prepared.source)
        kind = prepared.intent_kind
        try:
            if kind in {_K.MAIL_SEND, _K.MAIL_DRAFT_SAVE, _K.MAIL_DRAFT_UPDATE}:
                return self._reconcile_compose(binding, prepared)
            if kind is _K.MAIL_DRAFT_DISCARD:
                return self._reconcile_discard(binding, prepared)
            if kind in {_K.MAIL_MOVE, _K.MAIL_TRASH}:
                return self._reconcile_move(binding, prepared)
            return self._reconcile_mark_read(binding, prepared)
        except ChamberlainError as error:
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail=f"{error.code.value}: {error.detail}"[:300]
            )

    def _provable(self, source: str, kind: IntentKind) -> bool:
        for capability in self.capabilities(source).capabilities:
            if kind.value in capability.absence_provable:
                return True
        return False

    def _reconcile_compose(
        self, binding: ThunderbirdBinding, prepared: Prepared
    ) -> ReconcileResult:
        plan = _plan(prepared, ComposePlan)
        kind = prepared.intent_kind
        common: dict[str, object] = {
            "account_id": binding.account_id,
            "mailbox": binding.mailbox,
            "fingerprint": plan.fingerprint,
            "sent_folder_id": plan.sent_folder_id,
            "draft_folder_id": plan.draft_folder_id,
            "inbox_folder_id": plan.inbox_folder_id,
            "since_ms": plan.since_ms,
        }
        if kind is _K.MAIL_SEND:
            reply = self._client.call(
                Command.RECONCILE_SEND, protocol.ReconcileSendArgs.model_validate(common)
            )
        elif kind is _K.MAIL_DRAFT_SAVE:
            reply = self._client.call(
                Command.RECONCILE_DRAFT_SAVE, protocol.ReconcileDraftSaveArgs.model_validate(common)
            )
        else:
            if plan.draft is None or plan.expected_raw_sha256 is None:
                return ReconcileResult(
                    status=ReconcileStatus.UNKNOWN, detail="draft update lost its draft"
                )
            reply = self._client.call(
                Command.RECONCILE_DRAFT_UPDATE,
                protocol.ReconcileDraftUpdateArgs.model_validate(
                    {**common, "old": plan.draft, "old_raw_sha256": plan.expected_raw_sha256}
                ),
            )
        result = _typed(reply, protocol.ReconcileComposeResult)
        # An exact content match is required (R1: Message-ID is not honoured). Several exact
        # copies in one place are ambiguous, never `present`.
        exact = [m for m in result.matches if m.exact]
        by_place = {
            place: [m for m in exact if m.where == place]
            for place in ("sent", "drafts", "outbox", "inbox")
        }
        verified = result.refreshed
        if kind is _K.MAIL_SEND:
            return self._settle_send(binding, result, by_place, verified)
        return self._settle_draft(binding, prepared, result, by_place["drafts"], verified)

    def _settle_send(
        self,
        binding: ThunderbirdBinding,
        result: protocol.ReconcileComposeResult,
        by_place: dict[str, list[protocol.Match]],
        refreshed: bool,
    ) -> ReconcileResult:
        sent, inbox = by_place["sent"], by_place["inbox"]
        queued = by_place["outbox"] + by_place["drafts"]
        if len(sent) > 1 or (not sent and len(inbox) > 1):
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="several matching copies")
        if sent:
            return self._present(binding, sent[0].header, sent[0].raw_sha256, refreshed)
        if inbox:
            # R11: a self-addressed send that reached the sender's own Inbox is delivered.
            return self._present(binding, inbox[0].header, None, refreshed)
        if queued:
            return ReconcileResult(
                status=ReconcileStatus.QUEUED, detail="waiting in Outbox or Drafts"
            )
        if not result.complete:
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="; ".join(result.errors)[:300]
            )
        # Never proof: a submission can succeed while the Sent copy fails to be filed.
        return ReconcileResult(status=ReconcileStatus.ABSENT, proof=False)

    def _settle_draft(
        self,
        binding: ThunderbirdBinding,
        prepared: Prepared,
        result: protocol.ReconcileComposeResult,
        drafts: list[protocol.Match],
        refreshed: bool,
    ) -> ReconcileResult:
        if len(drafts) > 1:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="several matching drafts")
        if drafts:
            return self._present(binding, drafts[0].header, drafts[0].raw_sha256, refreshed)
        if not result.complete:
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="draft folder scan incomplete"
            )
        if prepared.intent_kind is _K.MAIL_DRAFT_UPDATE and result.old_copies != 1:
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="neither the old nor the new draft was found"
            )
        proof = refreshed and self._provable(prepared.source, prepared.intent_kind)
        return ReconcileResult(status=ReconcileStatus.ABSENT, proof=proof)

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

    def _reconcile_discard(
        self, binding: ThunderbirdBinding, prepared: Prepared
    ) -> ReconcileResult:
        plan = _plan(prepared, DiscardPlan)
        reply = self._client.call(
            Command.RECONCILE_DRAFT_DISCARD,
            protocol.ReconcileDraftDiscardArgs(
                account_id=binding.account_id,
                mailbox=binding.mailbox,
                draft=plan.draft,
                expected_raw_sha256=plan.expected_raw_sha256,
            ),
        )
        result = _typed(reply, protocol.ReconcileDiscardResult)
        if result.composers_open == 0:
            return ReconcileResult(
                status=ReconcileStatus.PRESENT,
                receipt=ProviderReceipt(verification=Verification(client_verified=True)),
            )
        return ReconcileResult(
            status=ReconcileStatus.ABSENT,
            proof=self._provable(prepared.source, prepared.intent_kind),
        )

    def _reconcile_move(self, binding: ThunderbirdBinding, prepared: Prepared) -> ReconcileResult:
        plan = _plan(prepared, MovePlan)
        trash = prepared.intent_kind is _K.MAIL_TRASH
        args = {
            "account_id": binding.account_id,
            "mailbox": binding.mailbox,
            "ref": plan.ref,
            "destination_folder_id": plan.destination_folder_id,
        }
        if trash:
            reply = self._client.call(
                Command.RECONCILE_TRASH, protocol.ReconcileTrashArgs.model_validate(args)
            )
        else:
            reply = self._client.call(
                Command.RECONCILE_MOVE, protocol.ReconcileMoveArgs.model_validate(args)
            )
        result = _typed(reply, protocol.ReconcileMoveResult)
        source_now = len(result.source.messages)
        destination_now = len(result.destination.messages)
        covered = result.source.complete and result.destination.complete
        moved_in = (
            destination_now >= max(1, plan.destination_before)
            if trash
            else destination_now == plan.destination_before + 1
        )
        if covered and source_now == 0 and moved_in and destination_now > 0:
            newest = max(result.destination.messages, key=lambda h: h.date_ms)
            return self._present(binding, newest, None, result.refresh_verified)
        if (
            covered
            and source_now == plan.source_before
            and destination_now == plan.destination_before
        ):
            proof = result.refresh_verified and self._provable(
                prepared.source, prepared.intent_kind
            )
            return ReconcileResult(status=ReconcileStatus.ABSENT, proof=proof)
        return ReconcileResult(
            status=ReconcileStatus.UNKNOWN, detail="source and destination disagree"
        )

    def _reconcile_mark_read(
        self, binding: ThunderbirdBinding, prepared: Prepared
    ) -> ReconcileResult:
        plan = _plan(prepared, MarkPlan)
        reply = self._client.call(
            Command.RECONCILE_MARK_READ,
            protocol.ReconcileMarkReadArgs(
                account_id=binding.account_id, mailbox=binding.mailbox, refs=plan.refs
            ),
        )
        result = _typed(reply, protocol.ReconcileMarkReadResult)
        if any(item.copies != 1 for item in result.items):
            return ReconcileResult(
                status=ReconcileStatus.UNKNOWN, detail="a message is missing or doubled"
            )
        done = [item.read == plan.read for item in result.items]
        if all(done):
            return ReconcileResult(
                status=ReconcileStatus.PRESENT,
                receipt=ProviderReceipt(verification=Verification(client_verified=True)),
            )
        if not any(done):
            proof = result.refresh_verified and self._provable(
                prepared.source, prepared.intent_kind
            )
            return ReconcileResult(status=ReconcileStatus.ABSENT, proof=proof)
        return ReconcileResult(
            status=ReconcileStatus.UNKNOWN, detail="only some messages were marked"
        )


def _seconds(value: float) -> timedelta:
    return timedelta(seconds=value)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _ms_opt(moment: datetime | None) -> int | None:
    return None if moment is None else _ms(moment)
