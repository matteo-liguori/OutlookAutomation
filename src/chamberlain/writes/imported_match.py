"""Read-only match of an imported write against the live source (owner ruling).

An old journal's operation has no intent, so it cannot be sent again. Its summary says what it
was. These functions look for that item in the live source and answer one thing: found, or not
proven. Not found is never read as "did not happen": only a connector's proof of absence or the
owner's click frees a grant, and there is no prepared write to ask a connector about here.
Deletes and moves never match: the absence of an item proves nothing about them.
"""

import logging
from datetime import UTC, datetime, timedelta

from chamberlain.app.context import Context
from chamberlain.domain.calendar import CalendarQuery
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.mail import FolderRole, MailQuery
from chamberlain.schema.errors import ChamberlainError
from chamberlain.schema.safelog import log_failure
from chamberlain.writes.imported import OperationSummary, norm_title
from chamberlain.writes.models import OperationRow

log = logging.getLogger(__name__)
MATCHABLE = frozenset(
    {IntentKind.MAIL_SEND, IntentKind.CALENDAR_CREATE, IntentKind.CALENDAR_UPDATE}
)
SENT_LOOKBACK = timedelta(days=2)
MAX_READS = 8


def _instant(text: str) -> datetime | None:
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def found_live(ctx: Context, op: OperationRow, summary: OperationSummary) -> bool:
    """True only when the live source holds the item. Any doubt, any error: False."""
    if op.kind not in MATCHABLE:
        return False
    try:
        if op.kind is IntentKind.MAIL_SEND:
            return _sent_mail(ctx, op, summary)
        return _calendar_event(ctx, op, summary)
    except ChamberlainError:
        log_failure(log, "imported reconcile read failed")
        return False


def _calendar_event(ctx: Context, op: OperationRow, summary: OperationSummary) -> bool:
    start = _instant(summary.start or "")
    if start is None or not summary.title:
        return False
    port = ctx.sources.calendar_port(op.source_id)
    query = CalendarQuery(
        start=start - timedelta(days=1), end=start + timedelta(days=2), text=summary.title
    )
    wanted = norm_title(summary.title)
    cursor: str | None = None
    for _ in range(4):
        page = port.events(op.source_id, query, cursor)
        for event in page.items:
            fields = event.fields
            same_start = (
                fields.start.date() == start.date()
                if len(summary.start or "") == 10
                else fields.start == start
            )
            if same_start and norm_title(fields.title) == wanted:
                return True
        if page.cursor is None:
            return False
        cursor = page.cursor
    return False


def _sent_mail(ctx: Context, op: OperationRow, summary: OperationSummary) -> bool:
    if not summary.title:
        return False
    port = ctx.sources.mail_port(op.source_id)
    since = None if op.attempted_at is None else op.attempted_at - SENT_LOOKBACK
    wanted = norm_title(summary.title)
    people = {a.casefold() for a in (*summary.to, *summary.cc)}
    reads = 0
    for folder in port.folders(op.source_id):
        if folder.role is not FolderRole.SENT:
            continue
        page = port.search(
            op.source_id, MailQuery(folder_id=folder.id, text=summary.title, since=since, limit=50)
        )
        for item in page.items:
            if norm_title(item.subject) != wanted:
                continue
            if not people:
                return True
            if reads >= MAX_READS:
                return False
            reads += 1
            message = port.read(op.source_id, item.locator)
            seen = {a.address.casefold() for a in (*message.to, *message.cc)}
            if people & seen:
                return True
    return False
