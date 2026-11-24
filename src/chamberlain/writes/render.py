"""Harness-written descriptions of what a write or a grant would do.

Titles and summaries never carry remote text (E-8). The preview of an intent
carries the agent's own draft text, so it travels as `Content` (E-7).
"""

from chamberlain.domain.grants import StandingScope
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarUpdate,
    IntentKind,
    MailDraftDiscard,
    MailDraftSave,
    MailDraftUpdate,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
    WriteIntent,
)

PREVIEW_CHARS = 1_500

_VERB = {
    IntentKind.MAIL_SEND: "Send a message",
    IntentKind.MAIL_DRAFT_SAVE: "Save a draft",
    IntentKind.MAIL_DRAFT_UPDATE: "Update a draft",
    IntentKind.MAIL_DRAFT_DISCARD: "Discard a draft",
    IntentKind.MAIL_MOVE: "Move a message",
    IntentKind.MAIL_TRASH: "Move a message to the trash",
    IntentKind.MAIL_MARK_READ: "Change read state",
    IntentKind.CALENDAR_CREATE: "Create a calendar event",
    IntentKind.CALENDAR_UPDATE: "Change a calendar event",
    IntentKind.CALENDAR_DELETE: "Delete a calendar event",
}


def grant_title(intent: WriteIntent) -> str:
    """A short harness-written title (at most 80 characters)."""
    verb = _VERB[IntentKind(intent.kind)]
    if isinstance(intent, MailSend):
        count = len({*intent.to, *intent.cc, *intent.bcc})
        return f"{verb} to {count} recipient{'s' if count != 1 else ''}"
    return verb


def grant_summary(intent: WriteIntent) -> str:
    return f"{_VERB[IntentKind(intent.kind)]} on {intent.source}, exactly as previewed."


def describe_intent(intent: WriteIntent, *, limit: int | None = PREVIEW_CHARS) -> tuple[str, bool]:
    """The write as the harness will perform it, and whether it was cut short."""
    lines: list[str] = [f"{_VERB[IntentKind(intent.kind)]} ({intent.source})"]
    body = ""
    if isinstance(intent, MailSend | MailDraftSave | MailDraftUpdate):
        lines.append("To: " + ", ".join(intent.to))
        if intent.cc:
            lines.append("Cc: " + ", ".join(intent.cc))
        if intent.bcc:
            lines.append("Bcc: " + ", ".join(intent.bcc))
        lines.append("Subject: " + intent.subject)
        for attachment in intent.attachments:
            lines.append(f"Attachment: {attachment.filename} sha256 {attachment.sha256}")
        body = intent.body
    if isinstance(intent, MailMove):
        lines.append(f"To folder: {intent.to_folder}; mark read: {intent.mark_read}")
    elif isinstance(intent, MailTrash):
        lines.append("Destination: the recoverable trash folder")
    elif isinstance(intent, MailMarkRead):
        lines.append(f"Messages: {len(intent.messages)}; mark as read: {intent.read}")
    elif isinstance(intent, MailDraftDiscard):
        lines.append("Draft discarded: " + intent.draft)
    elif isinstance(intent, CalendarCreate | CalendarUpdate):
        event = intent.event
        lines.append(f"Title: {event.title}")
        lines.append(f"When: {event.start.isoformat()} to {event.end.isoformat()}")
        if event.attendees:
            lines.append("Attendees: " + ", ".join(event.attendees))
    elif isinstance(intent, CalendarDelete):
        lines.append("Event: " + intent.event_ref)
    text = "\n".join(lines) + (("\n\n" + body) if body else "")
    if limit is not None and len(text) > limit:
        return text[:limit], True
    return text, False


def describe_scope(scope: StandingScope) -> str:
    """The standing scope in full, for the confirmation page and the proposal preview."""
    parts = [
        f"Source: {scope.source}",
        "Kinds: " + ", ".join(sorted(kind.value for kind in scope.intent_kinds)),
    ]
    if scope.recipients:
        parts.append("Recipients: " + ", ".join(sorted(scope.recipients)))
    if scope.recipient_domains:
        parts.append("Recipient domains: " + ", ".join(sorted(scope.recipient_domains)))
    if scope.folders:
        parts.append("Folders: " + ", ".join(sorted(scope.folders)))
    parts.append(
        f"Mark read allowed: {scope.allow_mark_read}; repeats allowed: {scope.allow_repeat}"
    )
    if scope.max_per_day is not None:
        parts.append(f"At most {scope.max_per_day} a day")
    return "\n".join(parts)
