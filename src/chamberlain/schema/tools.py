"""Request and response shapes of the twelve tools.

Contract: docs/contracts/02-tools.md. One request and one response model per
tool; the registry (`schema/registry.py`) pairs them.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind, EventState
from chamberlain.domain.grants import GrantKind, GrantState, Requirement, StandingScope
from chamberlain.domain.intents import IntentKind, WriteIntent
from chamberlain.domain.mail import FolderRole
from chamberlain.domain.operations import OperationState, Verification
from chamberlain.limits import (
    ACTION_TITLE_MAX,
    CARD_GOAL_MAX,
    CARD_RULES_MAX,
    CARD_TITLE_MAX,
    COMPLEX_POSITION_MAX,
    COMPLEX_REASON_MAX,
    EVIDENCE_MAX,
    FIND_LIMIT_MAX,
    FIX_MAX,
    NEXT_WORK_ITEMS_MAX,
    NOTE_MAX,
    OUTCOME_MAX,
    PAGE_CHARS,
    REASON_MAX,
    START_WORK_ITEMS,
    SUMMARY_MAX,
)
from chamberlain.schema.base import (
    ActionHandle,
    ArtifactHandle,
    CalendarEventHandle,
    CardHandle,
    ContactHandle,
    EventHandle,
    EvidenceHandle,
    GrantHandle,
    Handle,
    MessageHandle,
    Model,
    NoteHandle,
    OperationHandle,
    SessionHandle,
    Sha256,
    SourceId,
    Timestamp,
)
from chamberlain.schema.envelope import Content, ContentLine, NextStep
from chamberlain.schema.errors import ErrorCode


class ToolRequest(Model):
    """Base of every request (T-3)."""

    session: SessionHandle | None = None


# --- start, next, guide (attention) -----------------------------------------


class SourceStatus(Model):
    id: SourceId
    kind: Literal["mail", "calendar", "contacts"]
    state: HealthState
    capabilities: tuple[str, ...] = ()


class WorkItem(Model):
    """One event, one line, one exact call (A-9)."""

    event: EventHandle
    kind: EventKind
    subject: Handle | SourceId | None = None
    summary: str = Field(max_length=SUMMARY_MAX)
    next: NextStep


class StartRequest(ToolRequest):
    agent: str | None = Field(default=None, max_length=40)
    purpose: Literal["interactive", "scheduled"] = "interactive"
    resume: SessionHandle | None = None
    scheduled_day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    """The local day a scheduled run was meant for, given by the wake adapter (R10)."""


class StartResponse(Model):
    session: SessionHandle
    now: Timestamp
    sources: tuple[SourceStatus, ...] = ()
    work: tuple[WorkItem, ...] = Field(default=(), max_length=START_WORK_ITEMS)
    more_work: int = Field(default=0, ge=0)


class NextRequest(ToolRequest):
    handled: tuple[EventHandle, ...] = ()
    older_mail: tuple[EventHandle, ...] = Field(
        default=(),
        description=(
            "Events about older mail that the owner said yes to reading (also marks them handled). "
            "Never fill this without the owner's yes."
        ),
    )
    """An old-mail question handled without being listed here is a no (A-23)."""
    limit: int = Field(default=5, ge=1, le=NEXT_WORK_ITEMS_MAX)
    cursor: str | None = None


class NextResponse(Model):
    work: tuple[WorkItem, ...] = Field(default=(), max_length=NEXT_WORK_ITEMS_MAX)
    remaining: int = Field(default=0, ge=0)


class GuideRequest(ToolRequest):
    id: str | None = Field(default=None, pattern=r"^[a-z0-9_.]{3,40}$")
    cursor: str | None = None


class GuideRule(Model):
    id: str
    version: int
    title: str = Field(max_length=60)
    text: str | None = None


class GuideResponse(Model):
    rules: tuple[GuideRule, ...] = ()


# --- find, open, keep, forget, card (memory) --------------------------------


class FindScope(StrEnum):
    CARDS = "cards"
    MAIL = "mail"
    CALENDAR = "calendar"
    CONTACTS = "contacts"
    STORED = "stored"
    GRANTS = "grants"
    OPERATIONS = "operations"


class FindRequest(ToolRequest):
    scope: FindScope
    query: str | None = Field(default=None, max_length=200)
    within: Handle | None = None
    source: SourceId | None = None
    since: Timestamp | None = None
    """Mail: without it the last 90 days are searched; give an earlier date to look further back."""
    until: Timestamp | None = None
    unread: bool | None = None
    folder: FolderRole | None = None
    """Mail only: limit to the folder with this role (for example the inbox)."""
    limit: int = Field(default=10, ge=1, le=FIND_LIMIT_MAX)
    cursor: str | None = None


class Hit(Model):
    """A handle and one line. Never a body (T-8)."""

    handle: Handle
    kind: str
    when: Timestamp | None = None
    summary: str | None = Field(default=None, max_length=SUMMARY_MAX)
    content: ContentLine | None = None
    offset: int | None = Field(default=None, ge=0)
    suggested_card: CardHandle | None = None
    """Mail: an open card this message probably belongs to. A suggestion only."""


class FindResponse(Model):
    hits: tuple[Hit, ...] = Field(default=(), max_length=FIND_LIMIT_MAX)


class NoteView(Model):
    handle: NoteHandle
    text: str = Field(max_length=NOTE_MAX)


class ActionView(Model):
    kind: Literal["action"] = "action"
    handle: ActionHandle
    card: CardHandle
    title: str = Field(max_length=ACTION_TITLE_MAX)
    state: Literal["open", "done", "dropped"]
    due_at: Timestamp | None = None
    snooze_until: Timestamp | None = None
    important: bool = False
    """An important appointment: the owner acknowledges each reminder in the viewer."""
    outcome: str | None = Field(default=None, max_length=OUTCOME_MAX)
    notes: tuple[NoteView, ...] = ()


class CardView(Model):
    kind: Literal["card"] = "card"
    handle: CardHandle
    title: str
    goal: str = ""
    position: str = ""
    rules: str = ""
    state: Literal["open", "closed"]
    version: int = Field(ge=1)
    over_cap: bool = False
    review_at: Timestamp | None = None
    outcome: str | None = Field(default=None, max_length=OUTCOME_MAX)
    actions: tuple[ActionView, ...] = ()
    context: ArtifactHandle | None = None
    """Notes carried over from the old system, still stored. Not part of the card's size."""
    complex: Literal["pending", "approved", "declined"] | None = None
    """The owner's decision on a larger limit. `approved` raises the caps (M-36)."""


class GrantView(Model):
    kind: Literal["grant"] = "grant"
    handle: GrantHandle
    grant_kind: GrantKind
    state: GrantState
    title: str = Field(max_length=80)
    summary: str = Field(default="", max_length=300)
    source: SourceId
    requirement: Requirement
    content_sha256: Sha256
    card: CardHandle | None = None
    granted_at: Timestamp | None = None
    expires_at: Timestamp | None = None
    review_at: Timestamp | None = None


class OperationView(Model):
    kind: Literal["operation"] = "operation"
    handle: OperationHandle
    intent_kind: IntentKind
    source: SourceId
    state: OperationState
    grant: GrantHandle | None = None
    verification: Verification = Verification()
    may_have_happened: bool = False
    error_code: ErrorCode | None = None
    result_ref: Handle | None = None
    """Handle of what the write produced (a saved draft, a created event)."""
    items_ok: int = 0
    items_failed: int = 0
    warnings: tuple[Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,40}$")], ...] = ()
    """Harness-safe warning codes, for example `merged_existing_copy` (R7)."""
    settled_at: Timestamp | None = None
    recheck: (
        Annotated[
            str, StringConstraints(pattern=r"^(found|queued|missing)( \(looked in .{1,300}\))?$")
        ]
        | None
    ) = None
    """For an uncertain send: what the harness's one 24-hour look found and where it looked."""
    redo_of: OperationHandle | None = None
    """On a resend: the send it replaces."""
    redone_by: OperationHandle | None = None
    """On a send that was sent again: its resend."""


class EventView(Model):
    kind: Literal["event"] = "event"
    handle: EventHandle
    event_kind: EventKind
    subject: Handle | SourceId | None = None
    summary: str = Field(max_length=SUMMARY_MAX)
    state: EventState
    detected_at: Timestamp


class EvidenceView(Model):
    kind: Literal["evidence"] = "evidence"
    handle: EvidenceHandle
    text: str = Field(max_length=EVIDENCE_MAX)
    ref: Handle | None = None
    created_at: Timestamp


class ArtifactView(Model):
    kind: Literal["artifact"] = "artifact"
    handle: ArtifactHandle
    artifact_kind: Literal["mail_body", "attachment", "download", "import", "text"]
    media_type: str
    size: int = Field(ge=0)
    sha256: Sha256
    state: Literal["live", "released", "deleted"]
    reason: str = Field(max_length=REASON_MAX)
    owners: tuple[Handle, ...] = ()
    expires_at: Timestamp | None = None


class MessageView(Model):
    """Flags and times only; everything a sender wrote is in `content` (E-7)."""

    kind: Literal["message"] = "message"
    handle: MessageHandle
    source: SourceId
    folder_role: Literal["inbox", "sent", "drafts", "trash", "junk", "archive", "other"]
    when: Timestamp
    unread: bool
    attachment_count: int = Field(default=0, ge=0)
    raw_sha256: Sha256 | None = None
    cards: tuple[CardHandle, ...] = ()


class CalendarEventView(Model):
    kind: Literal["calendar_event"] = "calendar_event"
    handle: CalendarEventHandle
    source: SourceId
    start: Timestamp
    end: Timestamp
    all_day: bool = False
    status: Literal["confirmed", "tentative", "cancelled"]
    etag: str
    attendee_count: int = Field(default=0, ge=0)
    my_response: Literal["needs_action", "accepted", "tentative", "declined"] | None = None
    """Set when someone else invited this calendar's owner; the owner's answer so far."""
    repeats: Literal["occurrence", "series"] | None = None
    """`occurrence`: one date of a repeating event. `series`: the repeating event itself."""
    series: CalendarEventHandle | None = None
    """On an occurrence, the handle of its series: change or cancel every date through it."""


class ContactView(Model):
    kind: Literal["contact"] = "contact"
    handle: ContactHandle
    source: SourceId


View = Annotated[
    CardView
    | ActionView
    | GrantView
    | OperationView
    | EventView
    | EvidenceView
    | ArtifactView
    | MessageView
    | CalendarEventView
    | ContactView,
    Field(discriminator="kind"),
]


class AttachmentInfo(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    media_type: str
    size: int = Field(ge=0)
    content: ContentLine
    """The file name, which the sender chose (E-7)."""


class Range(Model):
    offset: int = Field(ge=0)
    length: int = Field(ge=0)
    total: int = Field(ge=0)


class Lease(Model):
    path: str
    expires_at: Timestamp


class OpenPart(StrEnum):
    SUMMARY = "summary"
    BODY = "body"
    HEADERS = "headers"
    ATTACHMENTS = "attachments"
    FILE = "file"


class OpenRequest(ToolRequest):
    handle: Handle
    part: OpenPart = OpenPart.SUMMARY
    attachment: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    """With a message handle and `part: body`: read this attachment's text instead."""
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=PAGE_CHARS, ge=1, le=PAGE_CHARS)
    lease: bool = False


class TextInfo(Model):
    """What the harness knows about text it pulled out of an attachment.

    Numbers and flags only, so it can never carry remote text (E-7, E-8).
    """

    format: Literal["pdf", "docx", "text"]
    pages: int | None = Field(default=None, ge=0)
    """PDF only: pages in the file."""
    pages_read: int | None = Field(default=None, ge=0)
    pages_without_text: tuple[int, ...] = Field(default=(), max_length=20)
    """PDF only: page numbers (from 1) with no text layer, for example a scan."""
    cut_short: bool = False
    """True when a page, size or character limit stopped the reading early."""


class OpenResponse(Model):
    handle: Handle
    view: View
    content: Content | None = None
    range: Range | None = None
    text_info: TextInfo | None = None
    attachments: tuple[AttachmentInfo, ...] = ()
    lease: Lease | None = None


class KeepKind(StrEnum):
    NOTE = "note"
    EVIDENCE = "evidence"
    ARTIFACT = "artifact"
    LABEL = "label"
    """The owner is a calendar event; the text is its category and, after a bar, a short title."""
    FINDING = "finding"
    """The owner is a `harness.review` item; the text is `idea` or `worrying`, a bar, a sentence."""


class KeepRequest(ToolRequest):
    kind: KeepKind
    owner: Handle
    reason: str = Field(min_length=1, max_length=REASON_MAX)
    text: str | None = Field(default=None, max_length=PAGE_CHARS)
    from_handle: Handle | None = None
    attachment: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    import_path: str | None = Field(default=None, max_length=1000)
    filename: str | None = Field(default=None, max_length=200, pattern=r"^[^\\/\r\n]+$")
    media_type: str | None = Field(default=None, max_length=100)
    expires_in_hours: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        given = [v for v in (self.text, self.from_handle, self.import_path) if v is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of text, from_handle or import_path")
        if self.kind in (KeepKind.NOTE, KeepKind.LABEL, KeepKind.FINDING) and self.text is None:
            raise ValueError(f"{self.kind.value}s are given as text")
        if self.kind is KeepKind.EVIDENCE and (
            self.import_path is not None or self.attachment is not None
        ):
            raise ValueError("evidence is given as text, or from an email handle")
        if self.attachment is not None and self.from_handle is None:
            raise ValueError("attachment names a part of from_handle")
        return self


class KeepResponse(Model):
    handle: Handle
    kind: KeepKind
    size: int = Field(ge=0)
    sha256: Sha256
    expires_at: Timestamp | None = None


class ForgetRequest(ToolRequest):
    handle: Handle
    owner: Handle | None = None


class ForgetResponse(Model):
    handle: Handle
    released: bool
    deleted: bool
    remaining_owners: int = Field(default=0, ge=0)


class CardOp(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    CLOSE = "close"
    REOPEN = "reopen"
    ADD_ACTION = "add_action"
    UPDATE_ACTION = "update_action"
    FINISH_ACTION = "finish_action"


class CardFields(Model):
    """Replaces the named fields; there is no append (T-12, M-14)."""

    title: str | None = Field(default=None, min_length=1, max_length=CARD_TITLE_MAX)
    goal: str | None = Field(default=None, max_length=CARD_GOAL_MAX)
    position: str | None = Field(default=None, max_length=COMPLEX_POSITION_MAX)
    """A normal card holds 1,500 characters here; only an approved complex card may use the rest."""
    rules: str | None = Field(default=None, max_length=CARD_RULES_MAX)


class ActionFields(Model):
    title: str | None = Field(default=None, min_length=1, max_length=ACTION_TITLE_MAX)
    due_at: Timestamp | None = None
    snooze_until: Timestamp | None = None
    important: bool | None = None
    """Mark an appointment reminder important (you decide, from the card's rules and context);
    due_at is the appointment's start and snooze_until when this reminder surfaces."""


_NEEDS_CARD = frozenset({CardOp.UPDATE, CardOp.CLOSE, CardOp.REOPEN, CardOp.ADD_ACTION})
_NEEDS_ACTION = frozenset({CardOp.UPDATE_ACTION, CardOp.FINISH_ACTION})
_NEEDS_OUTCOME = frozenset({CardOp.CLOSE, CardOp.FINISH_ACTION})


class CardRequest(ToolRequest):
    op: CardOp
    card: CardHandle | None = None
    action: ActionHandle | None = None
    expected_version: int | None = Field(default=None, ge=1)
    fields: CardFields | None = None
    action_fields: ActionFields | None = None
    outcome: str | None = Field(default=None, min_length=1, max_length=OUTCOME_MAX)
    complex_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=COMPLEX_REASON_MAX,
        description=(
            "With op update and no fields: ask the owner for a larger limit on a genuinely "
            "demanding card. Give a short reason. Nothing changes until the owner agrees."
        ),
    )

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.complex_reason is not None and (self.op is not CardOp.UPDATE or self.fields):
            raise ValueError("complex_reason goes with op update and no fields")
        if self.op in _NEEDS_CARD and self.card is None:
            raise ValueError(f"{self.op.value} needs card")
        if self.op in _NEEDS_ACTION and self.action is None:
            raise ValueError(f"{self.op.value} needs action")
        if self.op in _NEEDS_OUTCOME and self.outcome is None:
            raise ValueError(f"{self.op.value} needs outcome")
        if self.op is CardOp.CREATE and (self.fields is None or self.fields.title is None):
            raise ValueError("create needs fields.title")
        if self.op is CardOp.ADD_ACTION and (
            self.action_fields is None or self.action_fields.title is None
        ):
            raise ValueError("add_action needs action_fields.title")
        return self


class CardResponse(Model):
    card: CardView


# --- propose, do, reconcile (writes) ----------------------------------------


class ProposeRequest(ToolRequest):
    intent: WriteIntent | None = None
    standing: StandingScope | None = None
    operation: OperationHandle | None = None
    approval_evidence: EvidenceHandle | None = None
    redo_of: OperationHandle | None = None
    """Send an uncertain send again after the harness reported it missing (W-38)."""
    card: CardHandle | None = None
    evidence: tuple[EvidenceHandle, ...] = ()

    @model_validator(mode="after")
    def _one_mode(self) -> Self:
        modes = [
            v for v in (self.intent, self.standing, self.operation, self.redo_of) if v is not None
        ]
        if len(modes) != 1:
            raise ValueError("give exactly one of intent, standing, operation or redo_of")
        if (self.operation is None) != (self.approval_evidence is None):
            raise ValueError("operation and approval_evidence go together")
        return self


class ProposeResponse(Model):
    operation: OperationHandle | None = None
    grant: GrantHandle | None = None
    state: OperationState | GrantState
    requirement: Requirement
    ready: bool
    content_sha256: Sha256
    preview: Content


class DoRequest(ToolRequest):
    operation: OperationHandle


class DoResponse(Model):
    operation: OperationView


class ReconcileRequest(ToolRequest):
    operation: OperationHandle
    happened: Literal[True] | None = None
    """Say it happened, with `evidence` you recorded. Never say it did not."""
    evidence: EvidenceHandle | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> Self:
        if (self.happened is None) != (self.evidence is None):
            raise ValueError("happened and evidence go together")
        return self


class ReconcileResponse(Model):
    operation: OperationView
    changed: bool


# --- doctor (app) -----------------------------------------------------------


class DoctorCheck(StrEnum):
    ALL = "all"
    SOURCES = "sources"
    STORAGE = "storage"
    GUIDANCE = "guidance"
    AUDIT = "audit"
    SCHEMA = "schema"
    ACCESS = "access"


class DoctorRequest(ToolRequest):
    check: DoctorCheck = DoctorCheck.ALL
    metrics: bool = False


class CheckResult(Model):
    id: str = Field(pattern=r"^[a-z0-9_.]{3,40}$")
    status: Literal["ok", "warn", "fail"]
    summary: str = Field(max_length=SUMMARY_MAX)
    fix: str | None = Field(default=None, max_length=FIX_MAX)


class Metrics(Model):
    """The fixed keys of docs/contracts/10-targets.md."""

    open_cards: int = 0
    open_card_chars_avg: int = 0
    open_card_chars_max: int = 0
    evidence_rows: int = 0
    evidence_per_open_card: float = 0.0
    artifacts_live: int = 0
    artifact_bytes: int = 0
    artifacts_without_owner: int = 0
    orphans_quarantined: int = 0
    db_bytes: int = 0
    data_root_bytes: int = 0
    largest_reply_chars_7d: int = 0
    guidance_total_chars: int = 0
    uncertain_operations: int = 0
    grants_active: int = 0
    grants_awaiting_human: int = 0


class StoredBytesHistory(Model):
    """Stored bytes at the 4, 12 and 26 week targets; null until the history reaches them."""

    weeks_recorded: int = 0
    week_4: int | None = None
    week_12: int | None = None
    week_26: int | None = None


class DoctorResponse(Model):
    checks: tuple[CheckResult, ...] = ()
    metrics: Metrics | None = None
    stored_bytes_history: StoredBytesHistory | None = None
