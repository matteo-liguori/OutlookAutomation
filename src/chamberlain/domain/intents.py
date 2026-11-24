"""Write intents: the canonical, complete description of one write.

Contract: docs/contracts/04-writes.md (Intents, W-1 to W-5).
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from chamberlain.domain.calendar import EventFields
from chamberlain.limits import MARK_READ_MAX
from chamberlain.schema.base import (
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
    kind: Literal[IntentKind.MAIL_TRASH] = IntentKind.MAIL_TRASH
    source: SourceId
    message: MessageHandle
    internet_message_id: str | None = None
    from_folder: str | None = None


class MailMarkRead(Model):
    kind: Literal[IntentKind.MAIL_MARK_READ] = IntentKind.MAIL_MARK_READ
    source: SourceId
    messages: tuple[MessageHandle, ...] = Field(min_length=1, max_length=MARK_READ_MAX)
    read: bool = True


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


class CalendarDelete(Model):
    kind: Literal[IntentKind.CALENDAR_DELETE] = IntentKind.CALENDAR_DELETE
    source: SourceId
    event_ref: CalendarEventHandle
    expected_etag: str = Field(min_length=1)


MailIntent = (
    MailSend
    | MailDraftSave
    | MailDraftUpdate
    | MailDraftDiscard
    | MailMove
    | MailTrash
    | MailMarkRead
)
CalendarIntent = CalendarCreate | CalendarUpdate | CalendarDelete
WriteIntent = Annotated[MailIntent | CalendarIntent, Field(discriminator="kind")]

HARNESS_COMPLETED_FIELDS = frozenset({"message_id", "uid", "internet_message_id", "from_folder"})
"""Fields the agent leaves empty; the harness fills them before hashing (W-1, W-2)."""
