"""Cursor validation and paging for calendar collections."""

from collections.abc import Sequence

from chamberlain.domain.calendar import CalendarEvent, CalendarQuery, EventFields, EventStatus
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode


def bad_cursor() -> ChamberlainError:
    return ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid.")


def cursor_value(cursor: str, prefix: str) -> str:
    tag, separator, value = cursor.partition(".")
    if not separator or tag != prefix:
        raise bad_cursor()
    return value


def offset_cursor(cursor: str | None, prefix: str) -> int:
    if cursor is None:
        return 0
    number = cursor_value(cursor, prefix)
    if not number.isdigit():
        raise bad_cursor()
    try:
        return int(number)
    except ValueError:
        raise bad_cursor() from None


def matches_window(query: CalendarQuery, fields: EventFields, status: EventStatus) -> bool:
    if status is EventStatus.CANCELLED and not query.include_cancelled:
        return False
    if fields.end < query.start or fields.start > query.end:
        return False
    return (
        not query.text
        or query.text.casefold() in f"{fields.title}\n{fields.description}".casefold()
    )


def offset_page(
    found: list[CalendarEvent],
    gaps: Sequence[Gap],
    offset: int,
    limit: int,
    prefix: str,
) -> Page[CalendarEvent]:
    found.sort(key=lambda event: (event.fields.start, event.locator))
    end = offset + limit
    more = end < len(found)
    return Page[CalendarEvent](
        items=tuple(found[offset:end]),
        cursor=f"{prefix}.{end}" if more else None,
        complete=not more and not gaps,
        gaps=tuple(gaps[:1]),
    )
