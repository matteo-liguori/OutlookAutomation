"""Read-only London scheduling plans and recurrence readback verification."""

from datetime import date, datetime, time, timedelta, timezone
from .model import LONDON, now, stamp


def london_instant(day, clock, fold=None):
    naive = datetime.combine(date.fromisoformat(day), time.fromisoformat(clock))
    if naive.tzinfo or naive.second or naive.microsecond:
        raise ValueError("Use a local HH:MM time")
    options = {}
    for f in (0, 1):
        local = naive.replace(tzinfo=LONDON, fold=f)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(LONDON).replace(tzinfo=None) == naive:
            options[f] = utc
    if not options:
        raise ValueError("London time does not exist at the spring clock change")
    if len(set(options.values())) > 1 and fold is None:
        raise ValueError("Ambiguous London time; specify fold 0 or 1")
    if fold is not None and fold not in options:
        raise ValueError("Invalid clock-change fold")
    return options[fold] if fold is not None else options[min(options)]


def recurrence_instants(rrule, at):
    text = rrule.removeprefix("RRULE:")
    fields = dict(part.split("=", 1) for part in text.split(";"))
    if set(fields) != {"FREQ", "COUNT", "BYHOUR", "BYMINUTE"} or fields["FREQ"] != "DAILY":
        raise ValueError("Unsupported recurrence; provide exact one-shot daily rule")
    count = int(fields["COUNT"])
    hours = sorted(set(map(int, fields["BYHOUR"].split(","))))
    minutes = sorted(set(map(int, fields["BYMINUTE"].split(","))))
    if (
        not 1 <= count <= 8
        or any(not 0 <= h <= 23 for h in hours)
        or any(not 0 <= m <= 59 for m in minutes)
    ):
        raise ValueError("Invalid bounded recurrence")
    current = datetime.fromisoformat(stamp(at))
    result = []
    for offset in range(9):
        day = current.date() + timedelta(days=offset)
        for h in hours:
            for m in minutes:
                point = datetime.combine(day, time(h, m), timezone.utc)
                if point > current:
                    result.append(point)
        if len(result) >= count:
            return sorted(result)[:count]
    raise ValueError("Could not enumerate recurrence safely")


def plan(day=None, times=None, purpose="continuation", at=None, fold=None):
    current = datetime.fromisoformat(stamp(at or now()))
    local_now = current.astimezone(LONDON)
    day = day or local_now.date().isoformat()
    target = date.fromisoformat(day)
    clocks = times or (["14:00", "19:00"] if purpose == "continuation" else [])
    if not clocks or len(clocks) > 8 or len(set(clocks)) != len(clocks):
        raise ValueError("Provide one to eight distinct times")
    if purpose not in {"continuation", "reminder"}:
        raise ValueError("Unknown scheduling purpose")
    if purpose == "continuation" and set(clocks) - {"14:00", "19:00"}:
        raise ValueError("Mail continuations are only 14:00 and 19:00 London")
    if purpose == "continuation" and target != local_now.date():
        return dict(
            purpose=purpose,
            london_date=day,
            as_of=current.isoformat(),
            eligible=[],
            expired=clocks,
            rrule=None,
            reason="Continuation date is not today in London; no substitute created.",
        )
    if target < local_now.date():
        raise ValueError("Requested date has expired")
    points = sorted((london_instant(day, clock, fold), clock) for clock in clocks)
    eligible = [
        dict(london=point.astimezone(LONDON).isoformat(), utc=point.isoformat(), time=clock)
        for point, clock in points
        if point > current
    ]
    expired = [clock for point, clock in points if point <= current]
    rule = None
    if eligible:
        intended = [datetime.fromisoformat(p["utc"]) for p in eligible]
        hours = ",".join(map(str, sorted({p.hour for p in intended})))
        minutes = ",".join(map(str, sorted({p.minute for p in intended})))
        candidate = f"FREQ=DAILY;COUNT={len(intended)};BYHOUR={hours};BYMINUTE={minutes}"
        if recurrence_instants(candidate, current.isoformat()) == intended:
            rule = candidate
    return dict(
        purpose=purpose,
        london_date=day,
        as_of=current.isoformat(),
        eligible=eligible,
        expired=expired,
        rrule=rule,
        reason=(
            "Validated future instants only; no automation was created."
            if rule or not eligible
            else "Exact instants require explicit-date scheduling; a daily recurrence would fire on the wrong date or at extra times."
        ),
    )


def verify(payload):
    expected = payload["plan"]
    points = [datetime.fromisoformat(p["utc"]) for p in expected["eligible"]]
    if any(p.tzinfo is None for p in points):
        raise ValueError("Schedule instants need explicit offsets")
    regenerated = plan(
        expected["london_date"],
        [p["time"] for p in expected["eligible"]],
        expected["purpose"],
        expected["as_of"],
        points[0].astimezone(LONDON).fold if points else None,
    )
    if regenerated["eligible"] != expected["eligible"]:
        raise ValueError("Plan instants do not match their London date and wall times")
    actual = recurrence_instants(payload["actual_rrule"], expected["as_of"])
    if not points or actual != points:
        raise ValueError(
            "Schedule readback does not match the intended London instants; do not claim it is verified"
        )
    return {
        "verified": True,
        "london_date": expected["london_date"],
        "utc_instants": [p.isoformat() for p in actual],
    }
