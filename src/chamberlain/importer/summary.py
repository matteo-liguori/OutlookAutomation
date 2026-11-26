"""A display-and-matching summary of an old journal's request (owner ruling).

Reads only fields the old journal already holds: a calendar write's iCalendar text (SUMMARY,
DTSTART) and a mail write's subject and recipients. Bodies and attachments are never read.
Anything unreadable gives no summary, never an error.
"""

from datetime import UTC, date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import icalendar

from chamberlain.domain.timefmt import ts
from chamberlain.importer.convert import norm_ts, shorten
from chamberlain.importer.old import Json, OldApproval, OldJournal
from chamberlain.writes.imported import RECIPIENTS_MAX, SUMMARY_TEXT_MAX, OperationSummary

_TIME_KEYS = ("attempted_at", "sent_at", "at", "started_at", "captured_at")
_APPROVAL_TIME_KEYS = ("used_at", "consumed_at", "executed_at", "granted_at")


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, str | int) else ""


def _addresses(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found = [_text(item) for item in cast(list[Any], value)]
    return tuple(shorten(item, 120) for item in found if "@" in item)[:RECIPIENTS_MAX]


def _event(request: Json) -> tuple[str, str | None]:
    """`(SUMMARY, DTSTART)` from the new text, else from the version an update or delete expects."""
    for key in ("icalendar", "expected_icalendar"):
        raw = request.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed: Any = icalendar.Calendar.from_ical(raw)
            items: list[Any] = list(parsed.walk("VEVENT"))
        except ValueError:
            continue
        if not items:
            continue
        item = items[0]
        title = _text(item.get("SUMMARY", ""))
        start = item.get("DTSTART")
        if start is None:
            return title, None
        moment: Any = start.dt
        if isinstance(moment, datetime):
            aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
            return title, ts(aware)
        if isinstance(moment, date):
            return title, moment.isoformat()
        return title, None
    return "", None


def summarise(request: Json, calendar_label: str | None) -> OperationSummary | None:
    """The summary of one journal's `request`, or None when it says nothing useful."""
    if not request:
        return None
    if request.get("icalendar") or request.get("expected_icalendar"):
        title, start = _event(request)
        summary = OperationSummary(
            title=shorten(title, SUMMARY_TEXT_MAX),
            start=start,
            calendar=None if not calendar_label else shorten(calendar_label, SUMMARY_TEXT_MAX),
        )
    else:
        summary = OperationSummary(
            title=shorten(_text(request.get("subject")), SUMMARY_TEXT_MAX),
            to=_addresses(request.get("to")),
            cc=_addresses(request.get("cc")),
        )
    return None if summary.empty else summary


def attempted_at(journal: OldJournal, approval: OldApproval | None, zone: ZoneInfo) -> str | None:
    """When the old journal says the write was tried, in the stored form. None when it never did."""
    sources: list[Json] = [journal.state]
    result = journal.state.get("result")
    if isinstance(result, dict):
        sources.append(cast(Json, result))
    for source in sources:
        for key in _TIME_KEYS:
            stamp = norm_ts(source.get(key), zone)
            if stamp:
                return stamp
    if approval is not None:
        for key in _APPROVAL_TIME_KEYS:
            stamp = norm_ts(approval.details.get(key), zone)
            if stamp:
                return stamp
    return None
