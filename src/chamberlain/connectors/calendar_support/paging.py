"""Cursor validation and paging for calendar collections."""

import base64
from collections.abc import Sequence

from pydantic import BaseModel

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


def encode_token(token: str, prefix: str) -> str:
    return f"{prefix}." + base64.urlsafe_b64encode(token.encode()).decode()


def decode_token(cursor: str, prefix: str) -> str:
    token = cursor_value(cursor, prefix)
    try:
        return base64.b64decode(token, altchars=b"-_", validate=True).decode()
    except ValueError:
        raise bad_cursor() from None


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


def slice_page[T: BaseModel](
    page: type[Page[T]],
    found: Sequence[T],
    gaps: Sequence[Gap],
    offset: int,
    limit: int,
    prefix: str,
) -> Page[T]:
    end = offset + limit
    more = end < len(found)
    return page(
        items=tuple(found[offset:end]),
        cursor=f"{prefix}.{end}" if more else None,
        complete=not more and not gaps,
        gaps=tuple(gaps[:1]),
    )


def offset_page(
    found: list[CalendarEvent],
    gaps: Sequence[Gap],
    offset: int,
    limit: int,
    prefix: str,
) -> Page[CalendarEvent]:
    found.sort(key=lambda event: (event.fields.start, event.locator))
    return slice_page(Page[CalendarEvent], found, gaps, offset, limit, prefix)
