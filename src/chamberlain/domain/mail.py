"""Mail types exchanged across MailPort. Connector-side: never agent-facing.

Contract: docs/contracts/08-ports.md (Reading). Text here is remote text; the
app wraps it in `Content` before it reaches a reply (E-7).
"""

from enum import StrEnum

from pydantic import Field

from chamberlain.schema.base import EmailAddress, Model, Sha256, SourceId, Timestamp


class FolderRole(StrEnum):
    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    ARCHIVE = "archive"
    OTHER = "other"


class Folder(Model):
    id: str
    name: str
    path: str
    role: FolderRole = FolderRole.OTHER


class Address(Model):
    address: EmailAddress
    name: str = ""


class AttachmentMeta(Model):
    id: str
    filename: str
    media_type: str
    size: int = Field(ge=0)


class MessageSummary(Model):
    """One physical copy of a message (P-7)."""

    source: SourceId
    locator: str
    """Stable across client restarts (P-6)."""
    folder_id: str
    internet_message_id: str
    subject: str
    sender: Address
    date: Timestamp
    unread: bool
    has_attachments: bool = False
    preview: str = ""
    to: tuple[Address, ...] = ()
    cc: tuple[Address, ...] = ()
    """Recipients as the folder listing shows them: Sent-history seeding reads these."""


class Message(MessageSummary):
    authentication_results: tuple[str, ...] = ()
    """Every `Authentication-Results` header, topmost first, each clipped. Only the topmost,
    added by the receiving provider, may be trusted (`domain/sender_auth.py`)."""
    body_text: str = ""
    raw_sha256: Sha256 | None = None
    """Hash of the stored message; drafts use it for optimistic concurrency."""
    attachments: tuple[AttachmentMeta, ...] = ()


class AttachmentBytes(Model):
    meta: AttachmentMeta
    data: bytes
    sha256: Sha256


class MailQuery(Model):
    folder_id: str | None = None
    text: str | None = None
    internet_message_id: str | None = None
    since: Timestamp | None = None
    until: Timestamp | None = None
    unread: bool | None = None
    limit: int = Field(default=25, ge=1, le=200)
