"""Configuration-driven, readback-verified plans for app-owned automations.

Only explicitly bound automation IDs are managed. The companion JS executor uses
the supported app tool; this module never writes the app's automation files.
"""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import re
import tomllib

from .configuration import location, resolve, snapshot, zone, lock, paths, atomic
from .model import now
from .timing import london_instant, plan, recurrence_instants


MARKER = '\n\n[Harness schedule configuration]\n'


def read(ident, home=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', ident):
        raise ValueError('Automation IDs must be plain app identifiers')
    home = Path(home or os.environ.get('CODEX_HOME', Path.home()/'.codex'))
    path = home/'automations'/ident/'automation.toml'
    if path.stat().st_size > 131072:
        raise ValueError('Automation definition exceeds inspection limit')
    with path.open('rb') as stream:
        value = tomllib.load(stream)
    if value.get('id') != ident or value.get('kind') not in {'cron', 'heartbeat'}:
        raise ValueError('Automation identity or kind could not be verified')
    return value


def token(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def arguments(a):
    result = dict(mode='update', id=a['id'], kind=a['kind'], name=a['name'],
                  prompt=a['prompt'], rrule=a['rrule'], status=a['status'])
    if a['kind'] == 'heartbeat':
        result['targetThreadId'] = a['target_thread_id']
    else:
        result.update(projectId=(a.get('target') or {}).get('project_id', a.get('project_id')),
                      model=a['model'], reasoningEffort=a['reasoning_effort'],
                      executionEnvironment=a.get('execution_environment', 'local'))
    if 'notification_policy' in a:
        result['notificationPolicy'] = a['notification_policy']
    return result


def _daily_matches(rule, clock, timezone):
    # A named timezone preserves wall time across DST; an unzoned rule cannot
    # prove which installation/app timezone the scheduler will apply.
    match = re.fullmatch(r'DTSTART;TZID=([^:]+):\d{8}T(\d{2})(\d{2})00\nRRULE:FREQ=DAILY', rule)
    return bool(match and match[1] == timezone and match[2]+':'+match[3] == clock)


def reconcile(root=None, home=None, at=None):
    folder = location(root)
    if folder is None:
        return dict(status='unmanaged', operations=[], issues=[])
    # Bypass an invocation snapshot: config set/reset must inspect the NEW YAML.
    from .extensions import effective
    config = effective(resolve(root=folder))
    settings = config.values['schedule']
    bindings = settings['automations']
    if not bindings:
        return dict(status='unmanaged', operations=[], issues=[],
                    instruction='Bind existing mail automations in schedule.automations to enable app synchronisation.')
    operations, issues, observed = [], [], []
    with snapshot(config):
        point = datetime.fromisoformat(at or now())
        day = point.astimezone(zone()).date()
        for binding in bindings:
            ident, role = binding['id'], binding['role']
            try:
                a = read(ident, home)
                if role == 'morning' and a['kind'] != 'cron':
                    raise ValueError('Morning binding must be an existing standalone cron automation')
                if role == 'continuations' and (a['kind'] != 'heartbeat' or a.get('target_thread_id') != binding['thread_id']):
                    raise ValueError('Continuation binding must retain its verified chat target')
                observed.append(dict(id=ident, status=a['status']))
                if a['status'] != 'ACTIVE':
                    continue  # A YAML edit never re-enables deliberately paused work.
                args = arguments(a)
                if role == 'morning':
                    clock = settings['morning']
                    # Validate nonexistent/ambiguous wall times before publishing.
                    london_instant(str(day), clock)
                    rule = f'DTSTART;TZID={zone().key}:{day:%Y%m%d}T{clock.replace(":", "")}00\nRRULE:FREQ=DAILY'
                    same = _daily_matches(a['rrule'], clock, zone().key)
                    note = f'Daily startup: {clock} {zone().key}. Read the effective YAML configuration at startup.'
                    template = config.render(config.get('templates.morning'))
                else:
                    target = binding['date']
                    if target > str(day):
                        raise ValueError('Future-day continuation binding is not eligible yet')
                    planned = plan(target, at=point.isoformat())
                    if not planned['eligible']:
                        operations.append(dict(id=ident, expected=token(a), arguments=dict(mode='delete', id=ident)))
                        continue
                    points = [p['utc'] for p in planned['eligible']]
                    first = datetime.fromisoformat(points[0]).strftime('%Y%m%dT%H%M%SZ')
                    rule = planned['rrule'] or ('DTSTART:'+first+'\nRRULE:FREQ=DAILY;COUNT=1\nRDATE:'+
                            ','.join(datetime.fromisoformat(p).strftime('%Y%m%dT%H%M%SZ') for p in points[1:]))
                    try:
                        same = [p.isoformat() for p in recurrence_instants(a['rrule'], point.isoformat())
                                if p > point] == points
                    except (ValueError, KeyError):
                        same = False
                    args['name'] = settings['title'].format(date=day)+' — '+', '.join(p['time'] for p in planned['eligible'])
                    note = f'Continuation date: {target}, timezone: {zone().key}, configured times: '+', '.join(settings['continuations'])+'.'
                    note += ' Stop after the final configured same-day check; never carry this heartbeat into another day.'
                    template = config.render(config.get('templates.followup')).format(date=target, timezone=zone().key)
                # Retain user instructions while superseding historical hardcoded
                # schedule/cleanup times in the old prompt.
                args['rrule'] = rule
                args['prompt'] = a['prompt'].split(MARKER)[0]+MARKER+template+'\n'+note+(
                    '\nThe effective customisations.yaml schedule and this section supersede older time/date '
                    'references above. Run schedule sync before mail work and finish any pending app updates '
                    'through the supported tool, then verify readback. Do not invent replacement wake-ups.')
                if not same or args['prompt'] != a['prompt'] or args['name'] != a['name']:
                    operations.append(dict(id=ident, expected=token(a), arguments=args))
            except FileNotFoundError:
                if role == 'morning':
                    issues.append(dict(id=ident, error='Bound morning automation is missing; do not recreate cancelled work.'))
                # Completed/deleted bounded continuations are not resurrected.
            except (OSError, ValueError, KeyError, TypeError) as exc:
                issues.append(dict(id=ident, error=str(exc)))
    if not operations and not issues:
        with lock(folder):
            _, path = paths(folder)
            state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            if state.get('schedule_notice') and not state['schedule_notice'].get('verified'):
                state['schedule_notice']['verified'] = True
                atomic(path, json.dumps(state,indent=2)+'\n')
    return dict(status='pending' if operations or issues else 'synced', operations=operations,
                issues=issues, observed=observed, timezone=config.values['timezone'],
                executor='monitor/integrations/sync_automations.js',
                instruction='Apply pending operations with the supported app automation tool using the shared executor; '
                            'then run schedule sync again to verify actual readback. Never claim synchronised while pending. '
                            'Re-read immediately before applying; abort if the configuration or automation changed.')


def preflight(request):
    current = reconcile()
    if current.get('operations') != request.get('operations') or current.get('issues'):
        raise ValueError('Configuration or app state changed; obtain a fresh schedule sync plan')
    return dict(verified=True)
