"""What an operation imported from an old journal shows and is matched by (owner ruling).

An old journal has no intent, so nothing can ever dispatch it. It keeps a small summary instead:
the title, the time, the calendar or the recipients. The summary is for display and for a
read-only match against the live source. It is never an intent, never a prepared write and never
holds a body. Contract: docs/contracts/04-writes.md.
"""

from pydantic import Field

from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarUpdate,
    IntentKind,
    MailSend,
    WriteIntent,
)
from chamberlain.domain.timefmt import ts
from chamberlain.schema.base import Model

ADVICE_CALENDAR = "Check the calendar for this event."
ADVICE_MAIL = "Check the Sent folder for this message."
SUMMARY_TEXT_MAX = 300
RECIPIENTS_MAX = 10


class OperationSummary(Model):
    """Display and matching text of one imported write. Mail bodies never appear."""

    title: str = Field(default="", max_length=SUMMARY_TEXT_MAX)
    """Mail subject, or the calendar event's SUMMARY."""
    start: str | None = Field(default=None, max_length=40)
    """Calendar DTSTART: `YYYY-MM-DDTHH:MM:SSZ`, or `YYYY-MM-DD` for an all-day event."""
    calendar: str | None = Field(default=None, max_length=SUMMARY_TEXT_MAX)
    to: tuple[str, ...] = Field(default=(), max_length=RECIPIENTS_MAX)
    cc: tuple[str, ...] = Field(default=(), max_length=RECIPIENTS_MAX)

    @property
    def empty(self) -> bool:
        return not (self.title or self.start or self.to)


def advice_for(kind: IntentKind) -> str:
    """What the owner can check themselves, per kind of write."""
    return ADVICE_CALENDAR if kind.value.startswith("calendar.") else ADVICE_MAIL


def parse_summary(text: str | None) -> OperationSummary | None:
    if not text:
        return None
    try:
        return OperationSummary.model_validate_json(text)
    except ValueError:
        return None


def norm_title(text: str) -> str:
    """Title for comparison: no reply prefixes, one case, one run of spaces."""
    stripped = text.strip()
    while True:
        head, sep, rest = stripped.partition(":")
        if sep and head.strip().casefold() in ("re", "fw", "fwd"):
            stripped = rest.strip()
            continue
        return " ".join(stripped.casefold().split())


def summary_of_intent(intent: WriteIntent) -> OperationSummary | None:
    """The same summary, taken from a new intent, to see whether it repeats an imported write."""
    if isinstance(intent, MailSend):
        return OperationSummary(
            title=intent.subject[:SUMMARY_TEXT_MAX],
            to=tuple(intent.to)[:RECIPIENTS_MAX],
            cc=tuple(intent.cc)[:RECIPIENTS_MAX],
        )
    if isinstance(intent, CalendarCreate | CalendarUpdate):
        fields = intent.event
        start = fields.start.date().isoformat() if fields.all_day else ts(fields.start)
        return OperationSummary(title=fields.title[:SUMMARY_TEXT_MAX], start=start)
    return None


def repeats(old: OperationSummary, new: OperationSummary) -> bool:
    """True when `new` looks like the same write as the imported `old`."""
    if not old.title or norm_title(old.title) != norm_title(new.title):
        return False
    if new.start is not None or old.start is not None:
        return old.start == new.start
    wanted = {a.casefold() for a in (*old.to, *old.cc)}
    return not wanted or bool(wanted & {a.casefold() for a in (*new.to, *new.cc)})
