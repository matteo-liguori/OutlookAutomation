"""RFC 5545 RRULE text to and from the plain `Recurrence` rule.

Contract: docs/contracts/08-ports.md (P-23). Only the plain subset is read: a frequency, an
interval, a count or an until, and plain weekdays on a weekly rule. Anything else (a day of the
month, an Nth weekday, a set position) reads as None, so a series with a rule this harness cannot
describe is still read and cancelled as a whole, and its rule is never replaced from a guess.
"""

from datetime import UTC, date, datetime

from chamberlain.domain.calendar import WEEKDAY_ORDER, Frequency, Recurrence, Weekday

_FREQUENCIES = {f.value.upper(): f for f in Frequency}
_DAY_CODES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_CODE_OF = {WEEKDAY_ORDER[i]: code for code, i in _DAY_CODES.items()}
_ALLOWED = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}


def _until(text: str) -> datetime | None:
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_rrule(text: str, *, zone: str | None = None) -> Recurrence | None:
    """The plain rule an RRULE says, or None when it says more than that."""
    body = text.strip()
    if body.upper().startswith("RRULE:"):
        body = body[6:]
    parts: dict[str, str] = {}
    for piece in body.split(";"):
        key, _, value = piece.partition("=")
        key = key.strip().upper()
        if not key or key not in _ALLOWED or key in parts:
            return None
        parts[key] = value.strip()
    frequency = _FREQUENCIES.get(parts.get("FREQ", "").upper())
    if frequency is None or parts.get("WKST", "MO").upper() != "MO":
        return None
    weekdays: list[Weekday] = []
    if "BYDAY" in parts:
        for code in parts["BYDAY"].upper().split(","):
            if code not in _DAY_CODES or frequency is not Frequency.WEEKLY:
                return None
            weekdays.append(WEEKDAY_ORDER[_DAY_CODES[code]])
    until = _until(parts["UNTIL"]) if "UNTIL" in parts else None
    if "UNTIL" in parts and until is None:
        return None
    try:
        return Recurrence.model_validate(
            {
                "frequency": frequency,
                "interval": int(parts.get("INTERVAL", "1")),
                "weekdays": weekdays,
                "until": until,
                "count": int(parts["COUNT"]) if "COUNT" in parts else None,
                "zone": zone,
            }
        )
    except ValueError:
        return None


def format_rrule(rule: Recurrence, *, all_day: bool) -> str:
    """The RRULE value (no `RRULE:` prefix) for a plain rule."""
    parts = [f"FREQ={rule.frequency.value.upper()}"]
    if rule.interval != 1:
        parts.append(f"INTERVAL={rule.interval}")
    if rule.weekdays:
        parts.append("BYDAY=" + ",".join(_CODE_OF[d] for d in rule.weekdays))
    if rule.count is not None:
        parts.append(f"COUNT={rule.count}")
    if rule.until is not None:
        stamp = rule.until.astimezone(UTC)
        parts.append("UNTIL=" + stamp.strftime("%Y%m%d" if all_day else "%Y%m%dT%H%M%SZ"))
    return ";".join(parts)


def same_rule(
    actual: Recurrence | None, wanted: Recurrence, first: date, *, all_day: bool = False
) -> bool:
    """The read-back carries the rule that was written. A weekly rule without weekdays repeats on
    the first date's weekday, which a server may write out; the clock defaults to UTC. An all-day
    series ends on a date, so its `until` is compared by date and no clock counts."""
    if actual is None:
        return False
    names = {wanted.frequency, actual.frequency}
    if len(names) != 1:
        return False

    def days(rule: Recurrence) -> tuple[Weekday, ...]:
        if rule.weekdays or rule.frequency is not Frequency.WEEKLY:
            return rule.weekdays
        return (WEEKDAY_ORDER[first.weekday()],)

    return (
        actual.interval == wanted.interval
        and days(actual) == days(wanted)
        and actual.count == wanted.count
        and _same_end(actual.until, wanted.until, all_day=all_day)
        and (all_day or (actual.zone or "UTC") == (wanted.zone or "UTC"))
    )


def _same_end(actual: datetime | None, wanted: datetime | None, *, all_day: bool) -> bool:
    if actual is None or wanted is None:
        return actual is wanted
    return actual.date() == wanted.date() if all_day else actual == wanted
