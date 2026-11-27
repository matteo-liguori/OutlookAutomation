"""Harness-written descriptions of what a write or a grant would do.

Titles and summaries never carry remote text (E-8). The preview of an intent
carries the agent's own draft text, so it travels as `Content` (E-7).
"""

from collections.abc import Mapping

from chamberlain.domain.grants import StandingScope
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarRespond,
    CalendarUpdate,
    IntentKind,
    MailDraftDiscard,
    MailDraftSave,
    MailDraftUpdate,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
    OutsideAction,
    OutsideCategory,
    WriteIntent,
)
from chamberlain.domain.recurrence import describe

PREVIEW_CHARS = 1_500
EVENT_ATTENDEES_REF = "_event_attendees"
"""Key in the stored refs: the event has attendees, with any known addresses comma-separated."""
EVENT_ORGANISER_REF = "_event_organiser"
"""Key in the stored refs: who organised the event a response answers (W-1)."""
EVENT_WHEN_REF = "_event_when"
"""Key in the stored refs: when the occurrence an update or delete names starts (W-42)."""
EVENT_REPEATS_REF = "_event_repeats"
"""Key in the stored refs: how the series an update or delete names repeats, in words (W-42)."""

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
    IntentKind.CALENDAR_RESPOND: "Answer a calendar invitation",
    IntentKind.OUTSIDE_ACTION: "Act outside Chamberlain",
}
CATEGORY_WORDS = {
    OutsideCategory.FORM_FILL: "filling in a form (not sending it)",
    OutsideCategory.FORM_SUBMIT: "sending a form",
    OutsideCategory.PORTAL_READ: "signing in to read something",
    OutsideCategory.DOWNLOAD: "downloading a document",
    OutsideCategory.OTHER: "something else",
}


def _verb(intent: WriteIntent) -> str:
    """What the write is, in words; says plainly when it reaches a repeating event (W-42)."""
    if isinstance(intent, CalendarCreate) and intent.event.recurrence is not None:
        return "Create a repeating calendar event"
    if isinstance(intent, CalendarUpdate | CalendarDelete) and intent.scope is not None:
        one = intent.scope == "occurrence"
        if isinstance(intent, CalendarDelete):
            return (
                "Cancel one date of a repeating event"
                if one
                else "Delete a repeating event, every date"
            )
        return (
            "Change one date of a repeating event"
            if one
            else "Change a repeating event, every date"
        )
    return _VERB[IntentKind(intent.kind)]


def grant_title(intent: WriteIntent) -> str:
    """A short harness-written title (at most 80 characters)."""
    verb = _verb(intent)
    if isinstance(intent, MailSend):
        count = len({*intent.to, *intent.cc, *intent.bcc})
        return f"{verb} to {count} recipient{'s' if count != 1 else ''}"
    if isinstance(intent, OutsideAction):
        return f"{verb}: {intent.where}"[:80]
    return verb


def grant_summary(intent: WriteIntent) -> str:
    if isinstance(intent, OutsideAction):
        return f"{intent.what} ({intent.where}), exactly as previewed."
    return f"{_verb(intent)} on {intent.source}, exactly as previewed."


def describe_intent(
    intent: WriteIntent,
    *,
    limit: int | None = PREVIEW_CHARS,
    refs: Mapping[str, str] | None = None,
) -> tuple[str, bool]:
    """The write as the harness will perform it, and whether it was cut short.

    `refs` are the operation's stored refs: they say who an update or delete of an existing
    event will also notify.
    """
    stored = refs or {}
    attendees = stored.get(EVENT_ATTENDEES_REF)
    notified = ""
    if attendees is not None:
        notified = ", ".join(address for address in attendees.split(",") if "@" in address)
        notified = notified or "attendee addresses unavailable"
    lines: list[str] = [f"{_verb(intent)} ({intent.source})"]
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
        if intent.high_importance:
            lines.append("Importance: high")
        if intent.reply_to is not None:
            lines.append("In reply to: " + intent.reply_to)
        body = intent.body
    if isinstance(intent, MailMove | MailTrash):
        lines.append(f"Messages: {len(intent.messages)} from one folder")
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
        if event.recurrence is not None:
            lines.append("Repeats: " + describe(event.recurrence))
        if isinstance(intent, CalendarUpdate) and intent.scope is not None:
            lines.extend(_reach(intent.scope, stored))
        if event.attendees:
            lines.append("Attendees (invited by the calendar): " + ", ".join(event.attendees))
        if isinstance(intent, CalendarUpdate) and notified:
            lines.append("Also told of the change: " + notified)
    elif isinstance(intent, CalendarDelete):
        lines.append("Event: " + intent.event_ref)
        if intent.scope is not None:
            lines.extend(_reach(intent.scope, stored))
        if notified:
            lines.append("Cancellation sent to: " + notified)
    elif isinstance(intent, CalendarRespond):
        lines.append(f"Answer: {intent.response}")
        lines.append("Sent to the organiser: " + (intent.organiser or "(unknown)"))
    elif isinstance(intent, OutsideAction):
        lines = [f"{_verb(intent)}: {intent.where}", f"What: {intent.what}"]
        if intent.enters:
            lines.append(f"It will enter or submit: {intent.enters}")
        lines.append(f"Kind: {CATEGORY_WORDS[intent.category]}")
        lines.append(
            "Chamberlain does not do this and cannot check it: the assistant does it and says "
            "afterwards what happened. It includes no password, no payment and no accepting "
            "of terms."
        )
    text = "\n".join(lines) + (("\n\n" + body) if body else "")
    if limit is not None and len(text) > limit:
        return text[:limit], True
    return text, False


def _reach(scope: str, stored: Mapping[str, str]) -> list[str]:
    """Which part of a repeating event a change reaches, as the owner reads it (W-42)."""
    if scope == "occurrence":
        when = stored.get(EVENT_WHEN_REF)
        date = f" ({when})" if when else ""
        return [f"Applies to: this date only{date}. Every other date stays as it is."]
    rule = stored.get(EVENT_REPEATS_REF)
    return ["Applies to: every date of the series" + (f" ({rule})" if rule else "") + "."]


def describe_scope(scope: StandingScope) -> str:
    """The standing scope in full, for the confirmation page and the proposal preview."""
    if scope.where is not None and scope.categories is not None:
        return _describe_outside_scope(scope, scope.where, scope.categories)
    parts = [
        f"Source: {scope.source}"
        if scope.sources is None
        else "Sources (one permission for all of them): " + ", ".join(sorted(scope.sources)),
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


def _describe_outside_scope(
    scope: StandingScope, where: str, categories: frozenset[OutsideCategory]
) -> str:
    """A standing permission for outside actions, in plain words (G-25)."""
    kinds = ", ".join(sorted(CATEGORY_WORDS[c] for c in categories))
    parts = [
        f"Where: {where}",
        f"Kinds of action: {kinds}",
        f"What it allows: {scope.summary}",
        "Only for the card it is bound to; it ends when that card closes.",
        "It never covers entering a password, paying or accepting terms.",
        "Chamberlain does not do these actions or check them: the assistant does them and reports.",
    ]
    if scope.max_per_day is not None:
        parts.append(f"At most {scope.max_per_day} a day")
    return "\n".join(parts)
