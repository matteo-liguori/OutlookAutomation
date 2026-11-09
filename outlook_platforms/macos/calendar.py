"""Expand native Outlook iCalendar exports without changing recurring events."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from zoneinfo import ZoneInfo

from icalendar import Calendar
import recurring_ical_events


def _instant(value, zone):
    if isinstance(value, datetime):
        return (value.replace(tzinfo=zone) if value.tzinfo is None else value).astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time(), zone).astimezone(timezone.utc)
    raise ValueError('Invalid calendar date')


def _key(event):
    uid = str(event.get('UID', ''))
    if not uid:
        raise ValueError('Calendar event has no UID')
    recurrence = event.get('RECURRENCE-ID')
    return uid, recurrence.to_ical().decode() if recurrence is not None else ''


def _version(event):
    changed = event.decoded('LAST-MODIFIED', datetime.min.replace(tzinfo=timezone.utc))
    return int(event.get('SEQUENCE', 0)), _instant(changed, timezone.utc)


def _semantic(event):
    copy = deepcopy(event)
    copy.pop('DTSTAMP', None)
    return copy.to_ical()


def expand_result(result, request):
    if result.get('status') != 'ok' or 'calendar_sources' not in result:
        return result
    result = dict(result)
    sources = result.pop('calendar_sources')
    calendar_ids = {source['calendar_id'] for source in sources}
    if len(calendar_ids)>1:
        parts=[expand_result(dict(result,calendar_sources=[source for source in sources if source['calendar_id']==calendar_id]),request) for calendar_id in sorted(calendar_ids)]
        rows=sorted([item for part in parts for item in part['items']],key=lambda item:(item['start'],item['calendar_id'],item['id']))
        count=sum(part['matched_count'] for part in parts);errors=[error for part in parts for error in part['errors']]
        result.update(items=rows[:request['limit']],matched_count=count,truncated=count>request['limit'],errors=errors,
                      recurrence_coverage='expanded-native-icalendar',bounded_complete=all(part['bounded_complete'] for part in parts) and count<=request['limit'],
                      timezone=request.get('timezone','Europe/London'),coverage='partial' if errors or count>request['limit'] else 'bounded-native-query')
        return result
    zone_name = request.get('timezone', 'Europe/London')
    zone = ZoneInfo(zone_name)
    first = datetime.fromisoformat(request['start']).astimezone(timezone.utc)
    last = datetime.fromisoformat(request['end']).astimezone(timezone.utc)
    merged = Calendar()
    merged.add('VERSION', '2.0')
    merged.add('PRODID', '-//OutlookAutomation//Native Mac//EN')
    merged.add('X-WR-TIMEZONE', zone_name)
    events, owners, timezones, errors = {}, {}, {}, list(result.get('errors', []))
    for source in sources:
        try:
            exported = Calendar.from_ical(source['icalendar'])
            native = source.get('native')
            if native and native.get('recurring') is False:
                components = exported.walk('VEVENT')
                if len(components) != 1:
                    raise ValueError('Nonrecurring export does not identify one event')
                component = components[0]
                if (str(component.get('SUMMARY', '')) != native['subject']
                    or _instant(component.decoded('DTSTART'), zone) != _instant(datetime.fromisoformat(native['start']), zone)
                    or _instant(component.decoded('DTEND'), zone) != _instant(datetime.fromisoformat(native['end']), zone)):
                    errors.append('Native calendar properties disagree with iCalendar export: ' + str(source['id']))
                    continue
            for component in exported.walk():
                if component.name == 'VTIMEZONE':
                    key = str(component.get('TZID'))
                    if key in timezones and timezones[key].to_ical() != component.to_ical():
                        raise ValueError('Conflicting timezone definitions')
                    timezones[key] = component
                elif component.name == 'VEVENT':
                    key = _key(component)
                    if key in events and _version(component) == _version(events[key]) and _semantic(component) != _semantic(events[key]):
                        raise ValueError('Conflicting event versions')
                    if key not in events or _version(component) > _version(events[key]):
                        events[key] = component
                        owners[key] = source
        except Exception:
            errors.append('Invalid or conflicting native iCalendar export: ' + str(source['id']))
    for component in [*timezones.values(), *events.values()]:
        merged.add_component(component)
    rows = []; expansion_capped=False
    try:
        durations=[]
        for component in events.values():
            if component.get('DTSTART') and component.get('DTEND'):
                durations.append(max(timedelta(0),_instant(component.decoded('DTEND'),zone)-_instant(component.decoded('DTSTART'),zone)))
        lookback=max(durations,default=timedelta(days=1))+timedelta(days=1)
        for index,event in enumerate(recurring_ical_events.of(merged).after(first-lookback)):
            if index>=50000:
                errors.append('Recurrence scan cap reached');expansion_capped=True;break
            start_value, end_value = event.decoded('DTSTART'), event.decoded('DTEND')
            start, end = _instant(start_value, zone), _instant(end_value, zone)
            if start>=last:break
            if str(event.get('STATUS', '')).upper() == 'CANCELLED':continue
            if not start < last or not end > first:
                continue
            key = _key(event)
            source = owners.get(key) or owners.get((key[0], ''))
            if source is None:
                raise ValueError('Occurrence has no native source')
            original = events.get((key[0], '')) or events.get(key)
            recurring = bool(original and any(field in original for field in ('RRULE', 'RDATE')))
            occurrence = event.get('RECURRENCE-ID')
            instance = occurrence.to_ical().decode() if occurrence is not None else start.isoformat()
            entry_id = source['id'] if not recurring else source['id'] + '@' + hashlib.sha256(instance.encode()).hexdigest()[:20]
            item = dict(id=entry_id, native_id=source['id'], calendar_id=source['calendar_id'],
                        ical_uid=key[0], recurrence_id=instance if recurring else None,
                        subject=str(event.get('SUMMARY', '')), start=start.isoformat(), end=end.isoformat(),
                        location=str(event.get('LOCATION', '')), all_day=not isinstance(start_value, datetime) or source.get('all_day') is True or str(event.get('X-MICROSOFT-CDO-ALLDAYEVENT','')).upper()=='TRUE',
                        recurring=recurring)
            if request.get('include_body'):
                item['plain_text'] = str(event.get('DESCRIPTION', ''))
            rows.append(item)
            if len(rows)>request['limit']:
                expansion_capped=True;break
    except Exception:
        errors.append('Native recurrence expansion failed; coverage is incomplete')
    unique = {(item['calendar_id'], item['id']): item for item in rows}
    rows = sorted(unique.values(), key=lambda item: (item['start'], item['calendar_id'], item['id']))
    limit = request['limit']
    result.update(items=rows[:limit], matched_count=len(rows), matched_count_lower_bound=expansion_capped,truncated=expansion_capped or len(rows) > limit,
                  errors=errors, recurrence_coverage='expanded-native-icalendar',
                  bounded_complete=bool(result.get('calendar_scan_complete')) and not errors and not expansion_capped and len(rows) <= limit,
                  native_source_count=len(sources),timezone=zone_name, coverage='bounded-native-query' if result.get('calendar_scan_complete') and not errors and len(rows) <= limit else 'partial')
    return result
