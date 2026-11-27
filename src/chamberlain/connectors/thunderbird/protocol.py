"""Bridge protocol v3: every message as a Pydantic model.

Contract: docs/contracts/12-bridge-v2.md. The JSON Schema these models emit
is the source of the add-on's generated TypeScript types (`typegen.py`).

A frame carries one JSON object. Handshake, request, chunk and response
messages are told apart by `t`. A request names a command from the closed
list; its `args` and the response `result` are validated against the model
registered for that command in `COMMAND_MODELS`.
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

PROTOCOL_VERSION = 3

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
"""A request or handshake ID, unique per message (contract section 3)."""
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(max_length=200_000)]
Short = Annotated[str, Field(max_length=2_000)]


class Command(StrEnum):
    """The closed command list. Nothing else is ever executed (section 5)."""

    STATUS = "status"
    FOLDERS = "folders"
    QUERY = "query"
    READ = "read"
    ATTACHMENT = "attachment"
    REFRESH = "refresh"
    CONTACTS = "contacts"
    CONTACT_READ = "contact_read"
    COMPOSE = "compose"
    CHANGE = "change"
    DRAFT_DISCARD = "draft_discard"
    COPIES = "copies"
    CALENDAR_LIST = "calendar_list"
    CALENDAR_QUERY = "calendar_query"
    CALENDAR_READ = "calendar_read"
    CALENDAR_WRITE = "calendar_write"


WRITE_COMMANDS = frozenset(
    {Command.COMPOSE, Command.CHANGE, Command.DRAFT_DISCARD, Command.CALENDAR_WRITE}
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


class _BridgeFrame(Model):
    v: int
    id: Id


class AddonHello(_BridgeFrame):
    """First message from the add-on after it connects to the host."""

    t: Literal["addon_hello"] = "addon_hello"
    addon_version: Annotated[str, Field(max_length=40)]
    calendar_available: bool


class ClientHello(_BridgeFrame):
    """First message from a local client; carries the session token."""

    t: Literal["client_hello"] = "client_hello"
    token: Annotated[str, Field(max_length=200)]


class Welcome(_BridgeFrame):
    """The host's answer to an authenticated, compatible client."""

    t: Literal["welcome"] = "welcome"
    host_version: Annotated[str, Field(max_length=40)]
    addon_version: Annotated[str, Field(max_length=40)]
    calendar_available: bool


class RefusalCode(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    VERSION_MISMATCH = "version_mismatch"
    ADDON_NOT_CONNECTED = "addon_not_connected"
    BAD_HELLO = "bad_hello"
    DUPLICATE_ID = "duplicate_id"


class Refusal(_BridgeFrame):
    """The host (or add-on) refuses to operate; nothing was executed."""

    t: Literal["refusal"] = "refusal"
    code: RefusalCode
    message: Annotated[str, Field(max_length=500)] = ""


class RequestFrame(_BridgeFrame):
    t: Literal["request"] = "request"
    cmd: str
    args: dict[str, JsonValue] = Field(default_factory=dict)


class ChunkFrame(_BridgeFrame):
    """One piece of an attachment, inside the request or response with this ID."""

    t: Literal["chunk"] = "chunk"
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


class ResponseFrame(_BridgeFrame):
    t: Literal["response"] = "response"
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


_FrameMessage = (
    AddonHello | ClientHello | Welcome | Refusal | RequestFrame | ChunkFrame | ResponseFrame
)
Frame = Annotated[_FrameMessage, Field(discriminator="t")]
FRAME_ADAPTER: TypeAdapter[_FrameMessage] = TypeAdapter(Frame)


def parse_frame(raw: bytes) -> _FrameMessage:
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
    internet_message_id: str
    subject: Text
    date_ms: int
    author: Short
    recipients: tuple[Short, ...] = ()
    cc: tuple[Short, ...] = ()
    bcc: tuple[Short, ...] = ()
    read: bool
    folder_id: str
    has_attachments: bool = False


class _Recipients(Model):
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()


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
    write_busy: bool
    calendar_available: bool
    composers_own: int | None = None
    """Composer windows the add-on opened that are still open (None: the add-on cannot count)."""
    composers_other: int | None = None
    """Every other composer window: the owner's own. Counted only, never touched."""


class FoldersResult(Model):
    folders: tuple[FolderInfo, ...]


class QueryArgs(AccountArgs):
    folder_id: str | None = None
    internet_message_id: str | None = None
    subject: Annotated[str, Field(max_length=500)] | None = None
    words: Annotated[str, Field(max_length=500)] | None = None
    """Every word must appear in the subject, the sender or the recipients (any case)."""
    unread: bool | None = None
    since_ms: int | None = None
    until_ms: int | None = None
    limit: Annotated[int, Field(ge=1, le=200)] = 25
    after: QueryCursor | None = None
    max_scan: Annotated[int, Field(ge=1, le=5000)] = 2000


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


class ReadResult(_Recipients):
    header: Header
    subject: Text
    body: Text
    attachments: tuple[AttachmentInfo, ...] = ()
    in_reply_to: str = ""
    references: str = ""
    raw_sha256: Hex64 | None = None
    authentication_results: tuple[Annotated[str, Field(max_length=2000)], ...] = ()
    """Every Authentication-Results header, topmost first (sender authentication)."""


class AttachmentArgs(AccountArgs):
    folder_id: str
    internet_message_id: str
    part_name: str
    max_bytes: Annotated[int, Field(ge=1)]


class AttachmentResult(Model):
    name: str
    media_type: str
    size: int
    sha256: Hex64
    chunks: int


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


class ComposeArgs(AccountArgs, _Recipients):
    mode: Literal["send", "draft_save", "draft_update"]
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
    """Open the saved draft in a composer window for the owner (drafts only). Otherwise no
    window opens: the add-on composes headless."""
    sent_copy: Literal["client", "server"] = "client"
    """A send: `server` files no client copy, because the server keeps its own."""

    draft: MessageRef | None = None
    expected_raw_sha256: Hex64 | None = None

    @model_validator(mode="after")
    def _draft_fields(self) -> Self:
        updating = self.mode == "draft_update"
        if (self.draft is not None) != updating or (
            self.expected_raw_sha256 is not None
        ) != updating:
            raise ValueError("draft and its expected hash are required only for a draft update")
        if self.mode == "send" and self.display_only:
            raise ValueError("a send cannot be display only")
        return self


class DraftDiscardArgs(AccountArgs):
    draft: MessageRef
    expected_raw_sha256: Hex64


MessageIds = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=1000)], ...],
    Field(min_length=1, max_length=500),
]


class ChangeGroup(Model):
    folder_id: str
    ids: MessageIds


class ChangeArgs(AccountArgs):
    items: Annotated[tuple[ChangeGroup, ...], Field(min_length=1, max_length=200)]
    move_to: str | None = None
    trash: bool = False
    set_read: bool | None = None

    @model_validator(mode="after")
    def _operation(self) -> Self:
        if len({group.folder_id for group in self.items}) != len(self.items):
            raise ValueError("a folder occurs more than once")
        if any(
            len({item.removeprefix("<").removesuffix(">") for item in group.ids}) != len(group.ids)
            for group in self.items
        ):
            raise ValueError("a message ID occurs more than once in a folder")
        if self.move_to is not None:
            if len(self.items) != 1 or self.items[0].folder_id == self.move_to:
                raise ValueError("move needs one source folder distinct from the destination")
            if self.trash and self.set_read is not None:
                raise ValueError("trash preserves the existing read state")
        elif (
            self.trash or self.set_read is None or sum(len(group.ids) for group in self.items) > 200
        ):
            raise ValueError("marking needs a read flag and accepts up to 200 messages")
        return self


class ComposeResult(Model):
    attachment_bytes_verified: bool
    dialogs: tuple[Annotated[str, Field(max_length=500)], ...] = ()
    header: Header | None = None
    raw_sha256: Hex64 | None = None
    old_copies_remaining: int = 0


class ChangeItem(Model):
    ref: MessageRef
    ok: bool
    code: str = ""


class ChangeResult(Model):
    items: tuple[ChangeItem, ...]
    verified: bool
    read_state_preserved: bool | None
    merged_existing_copy: bool


class DiscardResult(Model):
    closed: int


# --- copy observations (read-only) -----------------------------------------------------------


class Fingerprint(_Recipients):
    """What a sent or saved copy must look like (no Message-ID needed, R1)."""

    sender: str
    subject: Annotated[str, Field(max_length=500)]
    body_sha256: Hex64
    """SHA-256 of the body after `normalise_body` (CRLF to LF, trimmed)."""
    attachment_sha256: tuple[Hex64, ...] = ()
    high_importance: bool = False
    in_reply_to: str = ""
    token: str


MAX_JS_DATE_MS = 8_640_000_000_000_000


class CopyScope(Model):
    folder_id: str | None
    """None scans the sender's own messages in discovered local Outboxes."""
    ids: MessageIds | None = None
    subject: Annotated[str, Field(max_length=500)] | None = None
    since_ms: int | None = None
    until_ms: int | None = None
    inspect_draft: bool = False
    include_attachment_hashes: bool = False

    @model_validator(mode="after")
    def _selector(self) -> Self:
        if (self.ids is None) == (self.subject is None):
            raise ValueError("each copy scope needs IDs or a subject")
        if self.ids is not None:
            if self.folder_id is None or self.include_attachment_hashes:
                raise ValueError("ID lookups require a bound folder and no content scan")
            if (self.since_ms is not None or self.until_ms is not None) and (
                self.inspect_draft
                or self.since_ms is None
                or self.until_ms is None
                or not 0 <= self.since_ms <= self.until_ms <= MAX_JS_DATE_MS
            ):
                raise ValueError("bounded ID lookups need both date bounds and no draft scan")
        elif (
            self.until_ms is not None
            or self.since_ms is None
            or (self.folder_id is None and self.inspect_draft)
        ):
            raise ValueError("subject scans require a lower date bound and a folder for Drafts")
        return self


class CopiesArgs(AccountArgs):
    where: Annotated[tuple[CopyScope, ...], Field(min_length=1, max_length=200)]
    refresh: bool = False


class CopyFact(_Recipients):
    header: Header
    subject: str | None = None
    body_sha256: Hex64 | None = None
    attachment_sha256: tuple[Hex64, ...] = ()
    high_importance: bool | None = None
    in_reply_to: str = ""
    token: str = ""
    raw_sha256: Hex64 | None = None
    composers_open: int | None = None


class CopyScopeResult(Model):
    copies: tuple[CopyFact, ...]
    complete: bool


class CopiesResult(Model):
    scopes: tuple[CopyScopeResult, ...]
    refresh_verified: bool


# --- calendar (optional capability `calendar`) ----------------------------------------------


class CalendarInfo(Model):
    id: Annotated[str, Field(min_length=1, max_length=1000)]
    name: Short
    type: Annotated[str, Field(max_length=100)]
    read_only: bool
    disabled: bool


class CalendarListResult(Model):
    calendars: tuple[CalendarInfo, ...]


class CalendarItem(Model):
    """One event, or one occurrence of a series (`recurrence_id` set), as iCalendar text."""

    id: Annotated[str, Field(min_length=1, max_length=1000)]
    recurrence_id: Annotated[str, Field(max_length=40)] | None = None
    icalendar: Text
    own_appointment: Literal[True] | None = None
    """Thunderbird verified that its private, guestless item uses the calendar's own organizer."""


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


class CalendarWriteArgs(CalendarReadArgs):
    kind: Literal["create", "update", "delete"]
    icalendar: Text | None = None
    expected_icalendar: Text | None = None
    """The exact stored text the caller read; the version check (its hash is the etag)."""

    @model_validator(mode="after")
    def _write_fields(self) -> Self:
        if (self.icalendar is not None) != (self.kind != "delete"):
            raise ValueError("new event text is required only for create or update")
        if (self.expected_icalendar is not None) != (self.kind != "create"):
            raise ValueError("the expected event text is required only for update or delete")
        return self


class CalendarWriteResult(Model):
    item: CalendarItem | None
    """The read-back; None after a delete."""


COMMAND_MODELS: dict[Command, tuple[type[Model], type[Model]]] = {
    Command.STATUS: (StatusArgs, StatusResult),
    Command.FOLDERS: (AccountArgs, FoldersResult),
    Command.QUERY: (QueryArgs, QueryResult),
    Command.READ: (ReadArgs, ReadResult),
    Command.ATTACHMENT: (AttachmentArgs, AttachmentResult),
    Command.REFRESH: (RefreshArgs, RefreshResult),
    Command.CONTACTS: (ContactsArgs, ContactsResult),
    Command.CONTACT_READ: (ContactReadArgs, ContactReadResult),
    Command.COMPOSE: (ComposeArgs, ComposeResult),
    Command.CHANGE: (ChangeArgs, ChangeResult),
    Command.DRAFT_DISCARD: (DraftDiscardArgs, DiscardResult),
    Command.COPIES: (CopiesArgs, CopiesResult),
    Command.CALENDAR_LIST: (StatusArgs, CalendarListResult),
    Command.CALENDAR_QUERY: (CalendarQueryArgs, CalendarQueryResult),
    Command.CALENDAR_READ: (CalendarReadArgs, CalendarReadResult),
    Command.CALENDAR_WRITE: (CalendarWriteArgs, CalendarWriteResult),
}


def _validate(
    model: type[Model], data: dict[str, JsonValue], code: ErrorCode, message: str
) -> Model:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise ChamberlainError(code, message, detail=str(error)[:500]) from None


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
    return _validate(
        COMMAND_MODELS[command][0],
        args,
        ErrorCode.REQUEST_INVALID,
        "The bridge request was malformed.",
    )


def parse_result(cmd: Command, result: dict[str, JsonValue]) -> Model:
    """Validate a response result against the command's model."""
    return _validate(
        COMMAND_MODELS[cmd][1],
        result,
        ErrorCode.SOURCE_UNAVAILABLE,
        "The Thunderbird add-on answered in a shape this version does not understand.",
    )
