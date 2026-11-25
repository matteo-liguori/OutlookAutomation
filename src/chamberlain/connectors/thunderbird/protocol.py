"""Bridge protocol v2: every message as a Pydantic model.

Contract: docs/contracts/12-bridge-v2.md. The JSON Schema these models emit
is the source of the add-on's generated TypeScript types (`typegen.py`).

A frame carries one JSON object. Handshake, request, chunk and response
messages are told apart by `t`. A request names a command from the closed
list; its `args` and the response `result` are validated against the model
registered for that command in `ARGS_MODELS` and `RESULT_MODELS`.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

PROTOCOL_VERSION = 2

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
"""A request or handshake ID, unique per message (contract section 3)."""
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(max_length=200_000)]
Short = Annotated[str, Field(max_length=2_000)]


class Capability(StrEnum):
    """What the add-on says it can do; the client requires all of them."""

    READ = "read"
    SEARCH = "search"
    ATTACHMENTS = "attachments"
    SEND = "send"
    DRAFT = "draft"
    MOVE = "move"
    TRASH = "trash"
    MARK_READ = "mark_read"
    CONTACTS = "contacts"
    REFRESH = "refresh"
    CALENDAR = "calendar"
    """Optional: only a client that uses the calendar commands requires it."""


OPTIONAL_CAPABILITIES: frozenset[Capability] = frozenset({Capability.CALENDAR})
REQUIRED_CAPABILITIES: frozenset[Capability] = frozenset(Capability) - OPTIONAL_CAPABILITIES


class Command(StrEnum):
    """The closed command list. Nothing else is ever executed (section 5)."""

    STATUS = "status"
    ACCOUNTS = "accounts"
    FOLDERS = "folders"
    QUERY = "query"
    READ = "read"
    ATTACHMENT = "attachment"
    RESOLVE = "resolve"
    REFRESH = "refresh"
    CONTACTS = "contacts"
    CONTACT_READ = "contact_read"
    SEND = "send"
    DRAFT_SAVE = "draft_save"
    DRAFT_UPDATE = "draft_update"
    DRAFT_DISCARD = "draft_discard"
    MOVE = "move"
    TRASH = "trash"
    TRASH_MANY = "trash_many"
    MARK_READ = "mark_read"
    RECONCILE_SEND = "reconcile_send"
    RECONCILE_DRAFT_SAVE = "reconcile_draft_save"
    RECONCILE_DRAFT_UPDATE = "reconcile_draft_update"
    RECONCILE_DRAFT_DISCARD = "reconcile_draft_discard"
    RECONCILE_MOVE = "reconcile_move"
    RECONCILE_TRASH = "reconcile_trash"
    RECONCILE_TRASH_MANY = "reconcile_trash_many"
    RECONCILE_MARK_READ = "reconcile_mark_read"
    CALENDAR_LIST = "calendar_list"
    CALENDAR_QUERY = "calendar_query"
    CALENDAR_READ = "calendar_read"
    CALENDAR_CREATE = "calendar_create"
    CALENDAR_UPDATE = "calendar_update"
    CALENDAR_DELETE = "calendar_delete"


CALENDAR_COMMANDS: frozenset[Command] = frozenset(
    {
        Command.CALENDAR_LIST,
        Command.CALENDAR_QUERY,
        Command.CALENDAR_READ,
        Command.CALENDAR_CREATE,
        Command.CALENDAR_UPDATE,
        Command.CALENDAR_DELETE,
    }
)

WRITE_COMMANDS: frozenset[Command] = frozenset(
    {
        Command.SEND,
        Command.DRAFT_SAVE,
        Command.DRAFT_UPDATE,
        Command.DRAFT_DISCARD,
        Command.MOVE,
        Command.TRASH,
        Command.TRASH_MANY,
        Command.MARK_READ,
        Command.CALENDAR_CREATE,
        Command.CALENDAR_UPDATE,
        Command.CALENDAR_DELETE,
    }
)


class BridgeErrorCode(StrEnum):
    """Add-on error codes and what they mean to the connector (section 7)."""

    BAD_REQUEST = "bad_request"
    UNKNOWN_COMMAND = "unknown_command"
    ACCOUNT_MISMATCH = "account_mismatch"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    CHANGED = "changed"
    BUSY = "busy"
    TIMEOUT = "timeout"
    OFFLINE = "client_offline"
    NOT_VERIFIED = "not_verified"
    TOO_LARGE = "too_large"
    HASH_MISMATCH = "hash_mismatch"
    UNSUPPORTED = "unsupported"
    INTERNAL = "internal"


# --- handshake and framing messages ---------------------------------------------------------


class AddonHello(Model):
    """First message from the add-on after it connects to the host."""

    t: Literal["addon_hello"] = "addon_hello"
    v: int
    id: Id
    addon_version: Annotated[str, Field(max_length=40)]
    browser_version: Annotated[str, Field(max_length=40)] = ""
    capabilities: tuple[str, ...] = ()


class ClientHello(Model):
    """First message from a local client; carries the session token."""

    t: Literal["client_hello"] = "client_hello"
    v: int
    id: Id
    token: Annotated[str, Field(max_length=200)]
    client_version: Annotated[str, Field(max_length=40)] = ""
    require: tuple[str, ...] = ()


class Welcome(Model):
    """The host's answer to an authenticated, compatible client."""

    t: Literal["welcome"] = "welcome"
    v: int
    id: Id
    host_version: Annotated[str, Field(max_length=40)]
    addon_version: Annotated[str, Field(max_length=40)]
    capabilities: tuple[str, ...]


class RefusalCode(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    VERSION_MISMATCH = "version_mismatch"
    CAPABILITY_MISSING = "capability_missing"
    ADDON_NOT_CONNECTED = "addon_not_connected"
    BAD_HELLO = "bad_hello"
    DUPLICATE_ID = "duplicate_id"


class Refusal(Model):
    """The host (or add-on) refuses to operate; nothing was executed."""

    t: Literal["refusal"] = "refusal"
    v: int
    id: Id
    code: RefusalCode
    message: Annotated[str, Field(max_length=500)] = ""


class RequestFrame(Model):
    t: Literal["request"] = "request"
    v: int
    id: Id
    cmd: str
    args: dict[str, JsonValue] = Field(default_factory=dict)


class ChunkFrame(Model):
    """One piece of an attachment, inside the request or response with this ID."""

    t: Literal["chunk"] = "chunk"
    v: int
    id: Id
    name: Annotated[str, Field(min_length=1, max_length=64)]
    """Which attachment (the spec's `key`, or the downloaded part name)."""
    index: Annotated[int, Field(ge=0)]
    data: str
    """Base64 of at most `CHUNK_BYTES` raw bytes."""
    sha256: Hex64
    """Hash of this chunk's raw bytes."""


class BridgeError(Model):
    code: BridgeErrorCode
    message: Annotated[str, Field(max_length=500)] = ""
    mutation: Literal["none", "possible"] = "none"
    """`none` only when the add-on proves no mutating call was issued."""
    retryable: bool = False


class ResponseFrame(Model):
    t: Literal["response"] = "response"
    v: int
    id: Id
    cmd: str
    ok: bool
    result: dict[str, JsonValue] | None = None
    error: BridgeError | None = None
    chunks: int = 0
    """How many `chunk` frames with this ID precede the response."""

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if self.ok != (self.error is None) or self.ok != (self.result is not None):
            raise ValueError("a response carries a result exactly when it is ok")
        return self


Frame = Annotated[
    AddonHello | ClientHello | Welcome | Refusal | RequestFrame | ChunkFrame | ResponseFrame,
    Field(discriminator="t"),
]
FRAME_ADAPTER: TypeAdapter[
    AddonHello | ClientHello | Welcome | Refusal | RequestFrame | ChunkFrame | ResponseFrame
] = TypeAdapter(Frame)


def parse_frame(
    raw: bytes,
) -> AddonHello | ClientHello | Welcome | Refusal | RequestFrame | ChunkFrame | ResponseFrame:
    """Parse one frame body; any fault is a `source.unavailable` (never another exception)."""
    try:
        return FRAME_ADAPTER.validate_json(raw)
    except ValidationError as error:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE,
            "The Thunderbird bridge sent a message this version does not understand.",
            detail=str(error)[:500],
        ) from None


def dump_frame(frame: Model) -> bytes:
    """Serialise a frame body (compact JSON, UTF-8)."""
    return frame.model_dump_json(exclude_none=True).encode("utf-8")


# --- shared shapes ---------------------------------------------------------------------------


class AccountArgs(Model):
    """Every mail command names its account and sender identity; the add-on verifies both."""

    account_id: Annotated[str, Field(min_length=1, max_length=200)]
    mailbox: Annotated[str, Field(min_length=3, max_length=320)]


class MessageRef(Model):
    """One physical copy by stable identity; session integers never cross the bridge."""

    folder_id: Annotated[str, Field(min_length=1, max_length=1000)]
    internet_message_id: Annotated[str, Field(min_length=1, max_length=998)]
    """Bare form, without angle brackets."""


class Header(Model):
    session_id: int
    """Session-local diagnostic only; never an argument (P-6)."""
    internet_message_id: str
    subject: Text
    date_ms: int
    author: Short
    recipients: tuple[Short, ...] = ()
    cc: tuple[Short, ...] = ()
    bcc: tuple[Short, ...] = ()
    read: bool
    folder_id: str
    folder_path: str = ""
    special_use: tuple[str, ...] = ()
    has_attachments: bool = False
    priority: str = ""


class QueryCursor(Model):
    date_ms: int
    key: str


class AttachmentInfo(Model):
    part_name: str
    name: str
    size: int
    content_type: str = ""


class AttachmentSpec(Model):
    """An attachment that travels as `chunks` chunk frames after the request."""

    key: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    size: Annotated[int, Field(ge=0)]
    sha256: Hex64
    content_type: str = "application/octet-stream"
    chunks: Annotated[int, Field(ge=0)]


class FolderInfo(Model):
    id: str
    name: str
    path: str
    account_id: str
    special_use: tuple[str, ...] = ()


# --- reads -----------------------------------------------------------------------------------


class StatusArgs(Model):
    pass


class StatusResult(Model):
    addon_version: str
    browser_version: str
    write_busy: bool
    capabilities: tuple[str, ...]


class AccountsArgs(Model):
    pass


class IdentityInfo(Model):
    id: str
    email: str
    name: str = ""


class AccountInfo(Model):
    id: str
    type: str
    name: str
    identities: tuple[IdentityInfo, ...] = ()


class AccountsResult(Model):
    accounts: tuple[AccountInfo, ...]


class FoldersArgs(AccountArgs):
    include_outbox: bool = False
    """Also list the local Outbox folders (they belong to the local account)."""


class FoldersResult(Model):
    folders: tuple[FolderInfo, ...]


class QueryArgs(AccountArgs):
    folder_id: str | None = None
    internet_message_id: str | None = None
    text: Annotated[str, Field(max_length=500)] | None = None
    subject: Annotated[str, Field(max_length=500)] | None = None
    subject_contains: bool = False
    words: Annotated[str, Field(max_length=500)] | None = None
    """Every word must appear in the subject, the sender or the recipients (any case)."""
    unread: bool | None = None
    since_ms: int | None = None
    until_ms: int | None = None
    limit: Annotated[int, Field(ge=1, le=200)] = 25
    after: QueryCursor | None = None
    max_scan: Annotated[int, Field(ge=1, le=5000)] = 2000
    own_only: bool = False
    """Only messages whose author is `mailbox`; required for a local Outbox."""


class QueryResult(Model):
    messages: tuple[Header, ...]
    more: bool
    complete: bool
    """False when the scan stopped at `max_scan` before seeing everything."""
    scanned: int
    next: QueryCursor | None = None


class ReadArgs(AccountArgs):
    folder_id: str
    internet_message_id: str
    raw_hash: bool = False


class ReadResult(Model):
    header: Header
    subject: Text
    body: Text
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    attachments: tuple[AttachmentInfo, ...] = ()
    in_reply_to: str = ""
    references: str = ""
    raw_sha256: Hex64 | None = None
    authentication_results: tuple[Annotated[str, Field(max_length=2000)], ...] = ()
    """Every Authentication-Results header, topmost first (sender authentication)."""
    read_state_preserved: bool


class AttachmentArgs(AccountArgs):
    folder_id: str
    internet_message_id: str
    part_name: str
    max_bytes: Annotated[int, Field(ge=1)]


class AttachmentResult(Model):
    name: str
    size: int
    sha256: Hex64
    chunks: int
    read_state_preserved: bool


class ResolveArgs(AccountArgs):
    folder_id: str
    internet_message_id: str


class ResolveResult(Model):
    messages: tuple[Header, ...]
    complete: bool


class RefreshArgs(AccountArgs):
    folder_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=8)]
    timeout_ms: Annotated[int, Field(ge=500, le=15_000)] = 8_000


class FolderRefresh(Model):
    folder_id: str
    success: bool
    code: str = ""


class RefreshResult(Model):
    folders: tuple[FolderRefresh, ...]
    provider_callback_verified: bool


class ContactsArgs(Model):
    address_book_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=20)]
    text: Annotated[str, Field(min_length=1, max_length=200)]
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    skip: Annotated[int, Field(ge=0)] = 0
    max_items: Annotated[int, Field(ge=1, le=5000)] = 2000


class ContactInfo(Model):
    id: str
    book_id: str
    name: str
    emails: tuple[str, ...] = ()


class ContactsResult(Model):
    contacts: tuple[ContactInfo, ...]
    more: bool
    complete: bool
    scanned: int


class ContactReadArgs(Model):
    address_book_id: Annotated[str, Field(min_length=1, max_length=200)]
    contact_id: Annotated[str, Field(min_length=1, max_length=200)]


class ContactReadResult(Model):
    contact: ContactInfo


# --- writes ----------------------------------------------------------------------------------


class ComposeArgs(AccountArgs):
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: Annotated[str, Field(max_length=500, pattern=r"^[^\r\n]*$")]
    body: Text
    high_importance: bool = False
    sent_folder_id: str
    draft_folder_id: str
    token: Annotated[str, Field(min_length=1, max_length=998)]
    """The intent's Message-ID token; also written as `X-Chamberlain-Write-Token`."""
    attachments: tuple[AttachmentSpec, ...] = ()
    reply_to: MessageRef | None = None
    display_only: bool = False
    """Leave the saved draft's composer open (drafts only)."""


class SendArgs(ComposeArgs):
    pass


class DraftSaveArgs(ComposeArgs):
    pass


class DraftUpdateArgs(ComposeArgs):
    draft: MessageRef
    expected_raw_sha256: Hex64


class DraftDiscardArgs(AccountArgs):
    draft: MessageRef
    expected_raw_sha256: Hex64


class MoveArgs(AccountArgs):
    ref: MessageRef
    destination_folder_id: str
    mark_read: bool = False


class TrashArgs(AccountArgs):
    ref: MessageRef
    destination_folder_id: str


MessageIds = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=1000)], ...],
    Field(min_length=1, max_length=500),
]


class TrashManyArgs(AccountArgs):
    """Move many messages of one folder to Trash in one Thunderbird move (clean-up batches)."""

    folder_id: str
    internet_message_ids: MessageIds
    destination_folder_id: str


class MarkReadArgs(AccountArgs):
    refs: Annotated[tuple[MessageRef, ...], Field(min_length=1, max_length=200)]
    read: bool = True


class SendResult(Model):
    submitted: Literal[True] = True
    attachment_bytes_verified: bool
    dialogs: tuple[Annotated[str, Field(max_length=500)], ...] = ()
    """Thunderbird prompts on the composer the add-on answered at once (never left waiting)."""


class DraftResult(Model):
    header: Header
    raw_sha256: Hex64
    attachment_bytes_verified: bool
    old_copies_remaining: int = 0
    """Draft update: copies of the replaced draft still present after the save."""
    dialogs: tuple[Annotated[str, Field(max_length=500)], ...] = ()


class MoveResult(Model):
    message: Header
    read_state_preserved: bool
    merged_existing_copy: bool = False
    marked_read: bool = False
    original_read: bool


class MarkReadItem(Model):
    ref: MessageRef
    ok: bool
    was_read: bool | None = None
    code: str = ""


class MarkReadResult(Model):
    items: tuple[MarkReadItem, ...]


class TrashManyItem(Model):
    internet_message_id: str
    ok: bool
    code: str = ""
    """`not_found` or `ambiguous` when it was not moved (checked before the move)."""


class TrashManyResult(Model):
    items: tuple[TrashManyItem, ...]
    verified: bool
    """Every moved message left the folder and reached the destination (one scan each)."""


class DiscardResult(Model):
    closed: int
    saved_mail_deleted: Literal[False] = False


# --- reconcile twins (read-only) --------------------------------------------------------------


class Fingerprint(Model):
    """What a sent or saved copy must look like (no Message-ID needed, R1)."""

    sender: str
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: Annotated[str, Field(max_length=500)]
    body_sha256: Hex64
    """SHA-256 of the body after `normalise_body` (CRLF to LF, trimmed)."""
    attachment_sha256: tuple[Hex64, ...] = ()
    high_importance: bool = False
    in_reply_to: str = ""
    token: str


class ReconcileComposeArgs(AccountArgs):
    fingerprint: Fingerprint
    sent_folder_id: str
    draft_folder_id: str
    inbox_folder_id: str | None = None
    since_ms: int
    refresh: bool = True


class ReconcileSendArgs(ReconcileComposeArgs):
    pass


class ReconcileDraftSaveArgs(ReconcileComposeArgs):
    pass


class ReconcileDraftUpdateArgs(ReconcileComposeArgs):
    old: MessageRef
    old_raw_sha256: Hex64


class Match(Model):
    header: Header
    where: Literal["sent", "outbox", "drafts", "inbox", "other"]
    exact: bool
    """Envelope, body hash, attachments and priority all equal the fingerprint."""
    token_match: bool
    raw_sha256: Hex64 | None = None
    internet_message_id_matches_token: bool = False


class ReconcileComposeResult(Model):
    matches: tuple[Match, ...]
    complete: bool
    refreshed: bool
    errors: tuple[str, ...] = ()
    old_copies: int | None = None


class ReconcileDraftDiscardArgs(AccountArgs):
    draft: MessageRef
    expected_raw_sha256: Hex64


class ReconcileDiscardResult(Model):
    copies: int
    raw_sha256: Hex64 | None = None
    composers_open: int


class ReconcileMoveArgs(AccountArgs):
    ref: MessageRef
    destination_folder_id: str
    refresh: bool = True


class FolderCopies(Model):
    folder_id: str
    messages: tuple[Header, ...]
    complete: bool


class ReconcileMoveResult(Model):
    source: FolderCopies
    destination: FolderCopies
    refreshed: bool
    refresh_verified: bool


class ReconcileTrashArgs(ReconcileMoveArgs):
    pass


class ReconcileTrashManyArgs(AccountArgs):
    folder_id: str
    internet_message_ids: MessageIds
    destination_folder_id: str
    refresh: bool = True


class TrashCopies(Model):
    internet_message_id: str
    source: int
    destination: int


class ReconcileTrashManyResult(Model):
    items: tuple[TrashCopies, ...]
    complete: bool
    refreshed: bool
    refresh_verified: bool


class ReconcileMarkReadArgs(AccountArgs):
    refs: Annotated[tuple[MessageRef, ...], Field(min_length=1, max_length=200)]
    refresh: bool = True


class ReconcileReadItem(Model):
    ref: MessageRef
    copies: int
    read: bool | None = None


class ReconcileMarkReadResult(Model):
    items: tuple[ReconcileReadItem, ...]
    refreshed: bool
    refresh_verified: bool


# --- calendar (optional capability `calendar`) ----------------------------------------------


class CalendarListArgs(Model):
    pass


class CalendarInfo(Model):
    id: Annotated[str, Field(min_length=1, max_length=1000)]
    name: Short
    type: Annotated[str, Field(max_length=100)]
    read_only: bool
    disabled: bool
    fresh_as_of: Annotated[str, Field(max_length=40)] | None = None
    """ISO time of the last synchronisation, only when the client can say."""


class CalendarListResult(Model):
    calendars: tuple[CalendarInfo, ...]


class CalendarItem(Model):
    """One event, or one occurrence of a series (`recurrence_id` set), as iCalendar text."""

    id: Annotated[str, Field(min_length=1, max_length=1000)]
    recurrence_id: Annotated[str, Field(max_length=40)] | None = None
    icalendar: Text


class CalendarRef(Model):
    calendar_id: Annotated[str, Field(min_length=1, max_length=1000)]


class CalendarQueryArgs(CalendarRef):
    start: Annotated[str, Field(min_length=10, max_length=40)]
    end: Annotated[str, Field(min_length=10, max_length=40)]
    limit: Annotated[int, Field(ge=1, le=5000)]


class CalendarQueryResult(Model):
    items: tuple[CalendarItem, ...]
    complete: bool
    gaps: tuple[Short, ...] = ()


class CalendarReadArgs(CalendarRef):
    event_id: Annotated[str, Field(min_length=1, max_length=1000)]


class CalendarReadResult(Model):
    item: CalendarItem | None = None


class CalendarCreateArgs(CalendarReadArgs):
    icalendar: Text


class CalendarUpdateArgs(CalendarCreateArgs):
    expected_icalendar: Text
    """The exact stored text the caller read; the version check (its hash is the etag)."""


class CalendarDeleteArgs(CalendarReadArgs):
    expected_icalendar: Text


class CalendarWriteResult(Model):
    item: CalendarItem | None = None
    """The read-back; `None` after a delete."""
    timing_ms: dict[str, int] = Field(default_factory=lambda: {})
    """Milliseconds since the command began, per stage."""


ARGS_MODELS: dict[Command, type[Model]] = {
    Command.STATUS: StatusArgs,
    Command.ACCOUNTS: AccountsArgs,
    Command.FOLDERS: FoldersArgs,
    Command.QUERY: QueryArgs,
    Command.READ: ReadArgs,
    Command.ATTACHMENT: AttachmentArgs,
    Command.RESOLVE: ResolveArgs,
    Command.REFRESH: RefreshArgs,
    Command.CONTACTS: ContactsArgs,
    Command.CONTACT_READ: ContactReadArgs,
    Command.SEND: SendArgs,
    Command.DRAFT_SAVE: DraftSaveArgs,
    Command.DRAFT_UPDATE: DraftUpdateArgs,
    Command.DRAFT_DISCARD: DraftDiscardArgs,
    Command.MOVE: MoveArgs,
    Command.TRASH: TrashArgs,
    Command.TRASH_MANY: TrashManyArgs,
    Command.MARK_READ: MarkReadArgs,
    Command.RECONCILE_SEND: ReconcileSendArgs,
    Command.RECONCILE_DRAFT_SAVE: ReconcileDraftSaveArgs,
    Command.RECONCILE_DRAFT_UPDATE: ReconcileDraftUpdateArgs,
    Command.RECONCILE_DRAFT_DISCARD: ReconcileDraftDiscardArgs,
    Command.RECONCILE_MOVE: ReconcileMoveArgs,
    Command.RECONCILE_TRASH: ReconcileTrashArgs,
    Command.RECONCILE_TRASH_MANY: ReconcileTrashManyArgs,
    Command.RECONCILE_MARK_READ: ReconcileMarkReadArgs,
    Command.CALENDAR_LIST: CalendarListArgs,
    Command.CALENDAR_QUERY: CalendarQueryArgs,
    Command.CALENDAR_READ: CalendarReadArgs,
    Command.CALENDAR_CREATE: CalendarCreateArgs,
    Command.CALENDAR_UPDATE: CalendarUpdateArgs,
    Command.CALENDAR_DELETE: CalendarDeleteArgs,
}

RESULT_MODELS: dict[Command, type[Model]] = {
    Command.STATUS: StatusResult,
    Command.ACCOUNTS: AccountsResult,
    Command.FOLDERS: FoldersResult,
    Command.QUERY: QueryResult,
    Command.READ: ReadResult,
    Command.ATTACHMENT: AttachmentResult,
    Command.RESOLVE: ResolveResult,
    Command.REFRESH: RefreshResult,
    Command.CONTACTS: ContactsResult,
    Command.CONTACT_READ: ContactReadResult,
    Command.SEND: SendResult,
    Command.DRAFT_SAVE: DraftResult,
    Command.DRAFT_UPDATE: DraftResult,
    Command.DRAFT_DISCARD: DiscardResult,
    Command.MOVE: MoveResult,
    Command.TRASH: MoveResult,
    Command.TRASH_MANY: TrashManyResult,
    Command.MARK_READ: MarkReadResult,
    Command.RECONCILE_SEND: ReconcileComposeResult,
    Command.RECONCILE_DRAFT_SAVE: ReconcileComposeResult,
    Command.RECONCILE_DRAFT_UPDATE: ReconcileComposeResult,
    Command.RECONCILE_DRAFT_DISCARD: ReconcileDiscardResult,
    Command.RECONCILE_MOVE: ReconcileMoveResult,
    Command.RECONCILE_TRASH: ReconcileMoveResult,
    Command.RECONCILE_TRASH_MANY: ReconcileTrashManyResult,
    Command.RECONCILE_MARK_READ: ReconcileMarkReadResult,
    Command.CALENDAR_LIST: CalendarListResult,
    Command.CALENDAR_QUERY: CalendarQueryResult,
    Command.CALENDAR_READ: CalendarReadResult,
    Command.CALENDAR_CREATE: CalendarWriteResult,
    Command.CALENDAR_UPDATE: CalendarWriteResult,
    Command.CALENDAR_DELETE: CalendarWriteResult,
}


def parse_args(cmd: str, args: dict[str, JsonValue]) -> Model:
    """Validate request args for a command in the closed list; refuse any other command."""
    try:
        command = Command(cmd)
    except ValueError:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "The Thunderbird bridge has no such command.",
            detail=f"unknown command {cmd[:40]!r}",
        ) from None
    try:
        return ARGS_MODELS[command].model_validate(args)
    except ValidationError as error:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "The bridge request was malformed.",
            detail=str(error)[:500],
        ) from None


def parse_result(cmd: Command, result: dict[str, JsonValue]) -> Model:
    """Validate a response result against the command's model."""
    try:
        return RESULT_MODELS[cmd].model_validate(result)
    except ValidationError as error:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE,
            "The Thunderbird add-on answered in a shape this version does not understand.",
            detail=str(error)[:500],
        ) from None
