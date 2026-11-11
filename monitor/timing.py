from .configuration import resolve
from .configuration import zone
"""Read-only London scheduling plans and recurrence readback verification."""

from datetime import date, datetime, time, timedelta, timezone
import re
from .model import now, stamp


def london_instant(day, clock, fold=None):
    naive = datetime.combine(date.fromisoformat(day), time.fromisoformat(clock))
    if naive.tzinfo or naive.second or naive.microsecond:
        raise ValueError("Use a local HH:MM time")
    options = {}
    for f in (0, 1):
        local = naive.replace(tzinfo=zone(), fold=f)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(zone()).replace(tzinfo=None) == naive:
            options[f] = utc
    if not options:
        raise ValueError("London time does not exist at the spring clock change")
    if len(set(options.values())) > 1 and fold is None:
        raise ValueError("Ambiguous London time; specify fold 0 or 1")
    if fold is not None and fold not in options:
        raise ValueError("Invalid clock-change fold")
    return options[fold] if fold is not None else options[min(options)]


def recurrence_instants(rrule, at):
    lines = rrule.strip().splitlines()
    anchor = None
    if len(lines) == 2 and re.fullmatch(r'DTSTART:\d{8}T\d{6}Z', lines[0]):
        anchor = datetime.strptime(lines[0], 'DTSTART:%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        if not lines[1].startswith('RRULE:'):raise ValueError('Expected RRULE after UTC DTSTART')
        text = lines[1][6:]
    elif len(lines) == 1:
        text = lines[0].removeprefix('RRULE:')
    else:raise ValueError('Use a single bounded UTC DTSTART/RRULE pair')
    parts = [part.split('=', 1) for part in text.split(';')]
    if any(len(part) != 2 for part in parts) or len({p[0] for p in parts}) != len(parts):
        raise ValueError('Invalid or duplicate recurrence fields')
    fields = dict(parts)
    allowed = {'FREQ', 'COUNT', 'BYHOUR', 'BYMINUTE'}
    required = {'FREQ', 'COUNT'} if anchor else allowed
    if not required <= fields.keys() or fields.keys() - allowed or fields['FREQ'] != 'DAILY':
        raise ValueError("Unsupported recurrence; provide exact one-shot daily rule")
    count = int(fields["COUNT"])
    hours = sorted(set(map(int, fields.get('BYHOUR', str(anchor.hour) if anchor else '').split(','))))
    minutes = sorted(set(map(int, fields.get('BYMINUTE', str(anchor.minute) if anchor else '').split(','))))
    if (
        not 1 <= count <= 8
        or any(not 0 <= h <= 23 for h in hours)
        or any(not 0 <= m <= 59 for m in minutes)
    ):
        raise ValueError("Invalid bounded recurrence")
    current = anchor or datetime.fromisoformat(stamp(at))
    result = []
    for offset in range(9):
        day = current.date() + timedelta(days=offset)
        for h in hours:
            for m in minutes:
                point = datetime.combine(day, time(h, m, anchor.second if anchor else 0), timezone.utc)
                if (anchor and point >= anchor) or (not anchor and point > current):
                    result.append(point)
        if len(result) >= count:
            return sorted(result)[:count]
    raise ValueError("Could not enumerate recurrence safely")


def plan(day=None, times=None, purpose="continuation", at=None, fold=None):
    current = datetime.fromisoformat(stamp(at or now()))
    local_now = current.astimezone(zone())
    day = day or local_now.date().isoformat()
    target = date.fromisoformat(day)
    clocks = times or (resolve().get('schedule.continuations') if purpose == 'continuation' else [])
    if not clocks and purpose == 'continuation':
        return dict(purpose=purpose,local_date=day, timezone=zone().key,as_of=current.isoformat(),eligible=[],expired=[],rrule=None,one_shot_rules=[],reason='Continuations disabled')
    if not clocks or len(clocks) > 8 or len(set(clocks)) != len(clocks):
        raise ValueError("Provide one to eight distinct times")
    if purpose not in {"continuation", "reminder"}:
        raise ValueError("Unknown scheduling purpose")
    if purpose == "continuation" and set(clocks) - set(resolve().get('schedule.continuations')):
        raise ValueError("Mail continuations are only 14:00 and 19:00 London")
    if purpose == "continuation" and target != local_now.date():
        return dict(
            purpose=purpose,
            local_date=day, timezone=zone().key,
            as_of=current.isoformat(),
            eligible=[],
            expired=clocks,
            rrule=None,
            one_shot_rules=[],
            reason="Continuation date is not today in London; no substitute created.",
        )
    if target < local_now.date():
        raise ValueError("Requested date has expired")
    points = sorted((london_instant(day, clock, fold), clock) for clock in clocks)
    eligible = [
        dict(london=point.astimezone(zone()).isoformat(), utc=point.isoformat(), time=clock)
        for point, clock in points
        if point > current
    ]
    expired = [clock for point, clock in points if point <= current]
    rule = None
    if eligible:
        intended = [datetime.fromisoformat(p["utc"]) for p in eligible]
        hours = ",".join(map(str, sorted({p.hour for p in intended})))
        minutes = ",".join(map(str, sorted({p.minute for p in intended})))
        candidate = intended[0].strftime('DTSTART:%Y%m%dT%H%M%SZ') + f"\nRRULE:FREQ=DAILY;COUNT={len(intended)};BYHOUR={hours};BYMINUTE={minutes}"
        if recurrence_instants(candidate, current.isoformat()) == intended:
            rule = candidate
    return dict(
        purpose=purpose,
        local_date=day, timezone=zone().key,
        as_of=current.isoformat(),
        eligible=eligible,
        expired=expired,
        rrule=rule,
        one_shot_rules=[datetime.fromisoformat(p['utc']).strftime('DTSTART:%Y%m%dT%H%M%SZ')+'\nRRULE:FREQ=DAILY;COUNT=1' for p in eligible],
        reason=(
            "Validated future instants only; no automation was created."
            if rule or not eligible
            else "Use one_shot_rules for these individual instants; one combined rule would add unwanted times."
        ),
    )


def verify(payload):
    expected = payload["plan"]
    points = [datetime.fromisoformat(p["utc"]) for p in expected["eligible"]]
    if any(p.tzinfo is None for p in points):
        raise ValueError("Schedule instants need explicit offsets")
    regenerated = plan(
        expected["local_date"],
        [p["time"] for p in expected["eligible"]],
        expected["purpose"],
        expected["as_of"],
        points[0].astimezone(zone()).fold if points else None,
    )
    if regenerated["eligible"] != expected["eligible"]:
        raise ValueError("Plan instants do not match their London date and wall times")
    actual = recurrence_instants(payload["actual_rrule"], expected["as_of"])
    if not points or actual != points:
        raise ValueError(
            "Schedule readback does not match the intended London instants; do not claim it is verified"
        )
    result = {
        "verified": True,
        "local_date": expected["local_date"], "timezone": zone().key,
        "utc_instants": [p.isoformat() for p in actual],
    }
    if payload.get('triggered_at'):
        trigger = datetime.fromisoformat(stamp(payload['triggered_at']))
        intended = min(actual, key=lambda p: abs((trigger-p).total_seconds()))
        delay = (trigger-intended).total_seconds()
        if delay < 0:raise ValueError(f'Trigger fired {-delay:g} seconds before its intended instant')
        result['trigger_check'] = dict(scheduled_for=intended.isoformat(), triggered_at=trigger.isoformat(),
                                       delay_seconds=delay, status='on-time' if delay<=300 else 'late')
    return result
