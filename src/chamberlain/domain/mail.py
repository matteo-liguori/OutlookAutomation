"""Mail types exchanged across MailPort. Connector-side: never agent-facing.

Contract: docs/contracts/08-ports.md (Reading). Text here is remote text; the
app wraps it in `Content` before it reaches a reply (E-7).
"""

import re
from enum import StrEnum

from pydantic import Field

from chamberlain.schema.base import EmailAddress, Model, Sha256, SourceId, Timestamp


class FolderRole(StrEnum):
    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    JUNK = "junk"
    ARCHIVE = "archive"
    OTHER = "other"


def bare(message_id: str) -> str:
    """The Message-ID as the add-on uses it: no angle brackets."""
    text = message_id.strip()
    if text.startswith("<") and text.endswith(">"):
        return text[1:-1]
    return text


_REPLY_PREFIX = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)


def strip_reply_prefix(subject: str) -> str:
    return _REPLY_PREFIX.sub("", subject)


def normalise_subject(text: str) -> str:
    return " ".join(strip_reply_prefix(text).split()).lower()


def folded_message_id(value: str) -> str:
    return value.strip().strip("<>").strip().casefold()


def message_key(internet_message_id: str, locator: str) -> str:
    """One identity for every copy of a message: the bare, lower-cased Message-ID.

    A message with no Message-ID falls back to its locator, so it is still counted once.
    """
    bare = folded_message_id(internet_message_id)
    return bare if bare else f"loc:{locator}"


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
    in_reply_to: str = ""
    """Message-ID this replies to, when the connector has it cheaply; empty otherwise."""
    references: tuple[str, ...] = ()
    """Message-IDs of the thread, oldest first, when the connector has them cheaply."""


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
