"""Expanding and describing a plain repeat rule.

Contract: docs/contracts/08-ports.md (P-23 to P-27). Pure: no clock, no I/O. A connector whose
server does not expand a series itself (the fake provider) uses `expand`; every connector
names an occurrence by its original start with `occurrence_id`.
"""

from calendar import monthrange
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from chamberlain.domain.calendar import WEEKDAY_ORDER, Frequency, Recurrence, Weekday
from chamberlain.limits import RECURRENCE_EXPAND_MAX

_SCAN_MAX = 200_000
"""Most candidate dates one expansion looks at, so no rule can loop for long."""
_ID_TIME = "%Y%m%dT%H%M%SZ"
_ID_DATE = "%Y%m%d"


def occurrence_id(start: datetime, *, all_day: bool) -> str:
    """The stable name of one occurrence: its original start, as `20261130T090000Z` (a date
    for an all-day event). It does not change when the occurrence is moved."""
    moment = start.astimezone(UTC)
    return moment.strftime(_ID_DATE if all_day else _ID_TIME)


def parse_occurrence_id(text: str) -> tuple[datetime, bool] | None:
    """The original start an `occurrence_id` names, and whether it is an all-day date."""
    for pattern, all_day in ((_ID_TIME, False), (_ID_DATE, True)):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC), all_day
        except ValueError:
            continue
    return None


def _months(first: date, step: int) -> Iterator[date]:
    """The same day of the month every `step` months; months without that day are skipped."""
    index = 0
    while True:
        total = first.year * 12 + first.month - 1 + index * step
        year, month = divmod(total, 12)
        month += 1
        if first.day <= monthrange(year, month)[1]:
            yield date(year, month, first.day)
        index += 1


def _years(first: date, step: int) -> Iterator[date]:
    index = 0
    while True:
        year = first.year + index * step
        if first.day <= monthrange(year, first.month)[1]:
            yield date(year, first.month, first.day)
        index += 1


def _weeks(first: date, step: int, weekdays: tuple[Weekday, ...]) -> Iterator[date]:
    days = sorted(WEEKDAY_ORDER.index(d) for d in weekdays) or [first.weekday()]
    monday = first - timedelta(days=first.weekday())
    week = 0
    while True:
        base = monday + timedelta(weeks=week * step)
        for day in days:
            candidate = base + timedelta(days=day)
            if candidate >= first:
                yield candidate
        week += 1


def _days(first: date, rule: Recurrence) -> Iterator[date]:
    if rule.frequency is Frequency.DAILY:
        index = 0
        while True:
            yield first + timedelta(days=index * rule.interval)
            index += 1
    elif rule.frequency is Frequency.WEEKLY:
        yield from _weeks(first, rule.interval, rule.weekdays)
    elif rule.frequency is Frequency.MONTHLY:
        yield from _months(first, rule.interval)
    else:
        yield from _years(first, rule.interval)


def expand(
    rule: Recurrence,
    start: datetime,
    end: datetime,
    *,
    all_day: bool,
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """`(start, end)` of every occurrence overlapping the window, in time order, as UTC.

    `start` and `end` are the first occurrence. Times repeat on the rule's clock (`zone`, else
    UTC), so nine o'clock stays nine o'clock across a clock change. All-day events repeat on
    dates and keep their length in days. At most RECURRENCE_EXPAND_MAX are returned.
    """
    zone = ZoneInfo(rule.zone) if rule.zone and not all_day else UTC
    first_local = start.astimezone(zone)
    length = end - start
    found: list[tuple[datetime, datetime]] = []
    for index, day in enumerate(_days(first_local.date(), rule)):
        if index >= _SCAN_MAX:
            break
        if all_day:
            begin = datetime.combine(day, time(), tzinfo=UTC)
        else:
            begin = datetime.combine(day, first_local.timetz().replace(tzinfo=None), tzinfo=zone)
            begin = begin.astimezone(UTC)
        if rule.until is not None and begin > rule.until:
            break
        if begin > window_end:
            break
        if rule.count is not None and index >= rule.count:
            break
        finish = begin + length
        if finish >= window_start:
            found.append((begin, finish))
            if len(found) >= RECURRENCE_EXPAND_MAX:
                break
    return found


_NAMES = {
    Weekday.MON: "Mon",
    Weekday.TUE: "Tue",
    Weekday.WED: "Wed",
    Weekday.THU: "Thu",
    Weekday.FRI: "Fri",
    Weekday.SAT: "Sat",
    Weekday.SUN: "Sun",
}
_UNIT = {
    Frequency.DAILY: "day",
    Frequency.WEEKLY: "week",
    Frequency.MONTHLY: "month",
    Frequency.YEARLY: "year",
}


def describe(rule: Recurrence) -> str:
    """The rule in plain words, for the owner: `every 2 weeks on Mon and Wed, until 2026-12-31`."""
    unit = _UNIT[rule.frequency]
    text = f"every {unit}" if rule.interval == 1 else f"every {rule.interval} {unit}s"
    if rule.weekdays:
        names = [_NAMES[d] for d in rule.weekdays]
        if len(names) > 1:
            names = [", ".join(names[:-1]), names[-1]]
            text += " on " + " and ".join(names)
        else:
            text += " on " + names[0]
    if rule.until is not None:
        text += f", until {rule.until.date().isoformat()}"
    elif rule.count is not None:
        text += f", {rule.count} time{'s' if rule.count != 1 else ''}"
    else:
        text += ", with no end"
    if rule.zone:
        text += f" (times on {rule.zone} clocks)"
    return text
