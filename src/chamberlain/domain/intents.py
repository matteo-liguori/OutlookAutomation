"""Write intents: the canonical, complete description of one write.

Contract: docs/contracts/04-writes.md (Intents, W-1 to W-5).
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from chamberlain.domain.calendar import EventFields
from chamberlain.limits import MARK_READ_MAX, TRASH_BATCH_MAX
from chamberlain.schema.base import (
    OUTSIDE_SOURCE,
    ArtifactHandle,
    CalendarEventHandle,
    EmailAddress,
    MessageHandle,
    Model,
    Sha256,
    SourceId,
)


class IntentKind(StrEnum):
    MAIL_SEND = "mail.send"
    MAIL_DRAFT_SAVE = "mail.draft_save"
    MAIL_DRAFT_UPDATE = "mail.draft_update"
    MAIL_DRAFT_DISCARD = "mail.draft_discard"
    MAIL_MOVE = "mail.move"
    MAIL_TRASH = "mail.trash"
    MAIL_MARK_READ = "mail.mark_read"
    CALENDAR_CREATE = "calendar.create"
    CALENDAR_UPDATE = "calendar.update"
    CALENDAR_DELETE = "calendar.delete"
    CALENDAR_RESPOND = "calendar.respond"
    OUTSIDE_ACTION = "outside.action"


Subject = Annotated[str, Field(max_length=500, pattern=r"^[^\r\n]*$")]
"""No CR or LF (W-5)."""


class AttachmentRef(Model):
    """An attachment comes only from the artifact store, matched by hash (W-3)."""

    artifact: ArtifactHandle
    sha256: Sha256
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[^\\/\r\n]+$")


class _Compose(Model):
    source: SourceId
    to: tuple[EmailAddress, ...] = ()
    cc: tuple[EmailAddress, ...] = ()
    bcc: tuple[EmailAddress, ...] = ()
    subject: Subject
    body: str = Field(max_length=200_000)
    body_format: Literal["plain", "html"] = "plain"
    attachments: tuple[AttachmentRef, ...] = ()
    reply_to: MessageHandle | None = None
    high_importance: bool = False
    message_id: str | None = None
    """The RFC Message-ID. Assigned by the harness at propose time (W-2)."""


class MailSend(_Compose):
    kind: Literal[IntentKind.MAIL_SEND] = IntentKind.MAIL_SEND

    @model_validator(mode="after")
    def _sendable(self) -> Self:
        if not self.to:
            raise ValueError("a send needs at least one address in `to`")
        if not self.subject:
            raise ValueError("a send needs a subject")
        return self


class MailDraftSave(_Compose):
    kind: Literal[IntentKind.MAIL_DRAFT_SAVE] = IntentKind.MAIL_DRAFT_SAVE


class MailDraftUpdate(_Compose):
    kind: Literal[IntentKind.MAIL_DRAFT_UPDATE] = IntentKind.MAIL_DRAFT_UPDATE
    draft: MessageHandle
    expected_sha256: Sha256


class MailDraftDiscard(Model):
    kind: Literal[IntentKind.MAIL_DRAFT_DISCARD] = IntentKind.MAIL_DRAFT_DISCARD
    source: SourceId
    draft: MessageHandle
    expected_sha256: Sha256


class MailMove(Model):
    kind: Literal[IntentKind.MAIL_MOVE] = IntentKind.MAIL_MOVE
    source: SourceId
    message: MessageHandle
    to_folder: str = Field(min_length=1)
    mark_read: bool = False
    internet_message_id: str | None = None
    """Completed by the harness from the ref before hashing (W-1)."""
    from_folder: str | None = None
    """Completed by the harness from the ref before hashing (W-1)."""


class MailTrash(Model):
    """One message (`message`), or a batch from one folder (`messages`) moved together."""

    kind: Literal[IntentKind.MAIL_TRASH] = IntentKind.MAIL_TRASH
    source: SourceId
    message: MessageHandle | None = None
    messages: tuple[MessageHandle, ...] = Field(default=(), max_length=TRASH_BATCH_MAX)
    internet_message_id: str | None = None
    """Completed by the harness for a single message (W-1)."""
    from_folder: str | None = None
    """Completed by the harness; every message of a batch must be in this one folder."""

    @model_validator(mode="after")
    def _one_or_many(self) -> Self:
        if (self.message is None) == (not self.messages):
            raise ValueError("give either message or messages")
        if len(set(self.messages)) != len(self.messages):
            raise ValueError("messages repeats a handle")
        return self

    @property
    def handles(self) -> tuple[str, ...]:
        return (self.message,) if self.message is not None else self.messages


class MailMarkRead(Model):
    kind: Literal[IntentKind.MAIL_MARK_READ] = IntentKind.MAIL_MARK_READ
    source: SourceId
    messages: tuple[MessageHandle, ...] = Field(min_length=1, max_length=MARK_READ_MAX)
    read: bool = True


EventScope = Literal["occurrence", "series"]
"""Which part of a repeating event a change or cancel reaches (P-25): one occurrence, or the
whole series. A single event takes no scope."""


class CalendarCreate(Model):
    kind: Literal[IntentKind.CALENDAR_CREATE] = IntentKind.CALENDAR_CREATE
    source: SourceId
    event: EventFields
    uid: str | None = None
    """Assigned by the harness at propose time; the reconcile token (W-2)."""


class CalendarUpdate(Model):
    kind: Literal[IntentKind.CALENDAR_UPDATE] = IntentKind.CALENDAR_UPDATE
    source: SourceId
    event_ref: CalendarEventHandle
    expected_etag: str = Field(min_length=1)
    event: EventFields
    scope: EventScope | None = None
    """Needed for a repeating event: `occurrence` with an occurrence's handle, `series` with
    the series' handle (`view.series` on an occurrence). A single event takes none."""


class CalendarDelete(Model):
    kind: Literal[IntentKind.CALENDAR_DELETE] = IntentKind.CALENDAR_DELETE
    source: SourceId
    event_ref: CalendarEventHandle
    expected_etag: str = Field(min_length=1)
    scope: EventScope | None = None


class CalendarRespond(Model):
    """Answer an invitation received: change only this calendar owner's own answer."""

    kind: Literal[IntentKind.CALENDAR_RESPOND] = IntentKind.CALENDAR_RESPOND
    source: SourceId
    event_ref: CalendarEventHandle
    expected_etag: str = Field(min_length=1)
    response: Literal["accepted", "tentative", "declined"]
    organiser: EmailAddress | None = None
    """Who gets the answer. Completed by the harness from the stored event (W-1)."""


class OutsideCategory(StrEnum):
    FORM_FILL = "form_fill"
    FORM_SUBMIT = "form_submit"
    PORTAL_READ = "portal_read"
    DOWNLOAD = "download"
    OTHER = "other"


OneLine = Annotated[str, Field(pattern=r"^[^\r\n]*$")]


class OutsideAction(Model):
    """Something the assistant does outside Chamberlain's tools that changes something or acts
    as the owner (04-writes W-44). The harness never does it and never checks it (W-46)."""

    kind: Literal[IntentKind.OUTSIDE_ACTION] = IntentKind.OUTSIDE_ACTION
    source: Literal["outside:owner"] = OUTSIDE_SOURCE  # pyright: ignore[reportAssignmentType]
    where: OneLine = Field(min_length=1, max_length=80)
    """The site, portal or app."""
    what: OneLine = Field(min_length=1, max_length=300)
    """What it will do, in plain words."""
    enters: OneLine = Field(default="", max_length=300)
    """What it will enter or submit."""
    category: OutsideCategory
    enters_password: bool
    pays: bool
    accepts_terms: bool
    """The three things only the owner does (W-45). Always stated; true is refused."""


MailIntent = (
    MailSend
    | MailDraftSave
    | MailDraftUpdate
    | MailDraftDiscard
    | MailMove
    | MailTrash
    | MailMarkRead
)
CalendarIntent = CalendarCreate | CalendarUpdate | CalendarDelete | CalendarRespond
WriteIntent = Annotated[MailIntent | CalendarIntent | OutsideAction, Field(discriminator="kind")]

HARNESS_COMPLETED_FIELDS = frozenset(
    {"message_id", "uid", "internet_message_id", "from_folder", "organiser"}
)
"""Fields the agent leaves empty; the harness fills them before hashing (W-1, W-2)."""
