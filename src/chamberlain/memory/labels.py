"""A category for a calendar event, given by the agent with `keep` (kind `label`).

Contract: docs/contracts/02-tools.md (T-10) and 06-memory.md (event labels). The agent names a
calendar event handle and writes `category` or `category | short title`. The category is one of
seven fixed words; the short title stands in for a vague invite title in the viewer only (the
calendar itself is never changed). A label belongs to the event as a person sees it (title,
start, end, all day), so the same event on two calendars shares it.
"""

from datetime import UTC, timedelta

from chamberlain.app.context import Context, Result
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.handles import HandleKind, handle_kind
from chamberlain.domain.timefmt import ts
from chamberlain.memory import refs, repo
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import KeepKind, KeepRequest, KeepResponse
from chamberlain.store.db import transaction

CATEGORIES: tuple[str, ...] = (
    "work",
    "personal",
    "social",
    "travel",
    "health",
    "occasion",
    "home",
)
"""What each word means is in guidance (`calendar.label`); the viewer gives each a colour."""
TITLE_MAX = 120
RETENTION_DAYS = 365


def event_identity(fields: EventFields) -> str:
    """The same event on two calendars gives the same identity."""
    title = " ".join(fields.title.split()).casefold()
    start = fields.start.astimezone(UTC).isoformat()
    end = fields.end.astimezone(UTC).isoformat()
    return sha256_text("|".join([title, start, end, str(fields.all_day)]))


def last_day(fields: EventFields) -> str:
    """The UTC date the event ends on (an end exactly at midnight belongs to the day before)."""
    end = max(fields.start, fields.end - timedelta(seconds=1))
    return end.astimezone(UTC).date().isoformat()


def parse_label(text: str) -> tuple[str, str]:
    """`(category, title)` from the agent's text, or `request.invalid` saying what is allowed."""
    category, _, title = text.partition("|")
    word = category.strip().lower()
    short = " ".join(title.split())
    if word not in CATEGORIES or len(short) > TITLE_MAX:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            f"A label is one of {', '.join(CATEGORIES)}, then optionally a bar and a short "
            f"title of at most {TITLE_MAX} characters.",
            field="text",
        )
    return word, short


def keep_label(ctx: Context, request: KeepRequest) -> Result:
    """Give one calendar event a category. A repeat replaces the earlier label."""
    assert request.text is not None
    if handle_kind(request.owner) is not HandleKind.CALENDAR_EVENT:
        raise ChamberlainError(
            ErrorCode.REQUEST_WRONG_KIND,
            "A label goes on a calendar event. Find the event, then name its handle.",
            field="owner",
        )
    category, title = parse_label(request.text)
    source, locator = refs.locate(ctx.db, request.owner)
    event = ctx.sources.calendar_port(source).read(source, locator)
    with transaction(ctx.db):
        repo.upsert_event_label(
            ctx.db,
            identity=event_identity(event.fields),
            category=category,
            title=title,
            last_day=last_day(event.fields),
            now=ts(ctx.clock.now()),
        )
    stored = f"{category}|{title}"
    return Result(
        KeepResponse(
            handle=request.owner,
            kind=KeepKind.LABEL,
            size=len(stored.encode()),
            sha256=sha256_text(stored),
        )
    )


def trim(ctx: Context) -> int:
    """Delete labels of events that ended more than a year ago."""
    edge = (ctx.clock.now() - timedelta(days=RETENTION_DAYS)).date().isoformat()
    with transaction(ctx.db):
        return repo.delete_event_labels_before(ctx.db, edge)
