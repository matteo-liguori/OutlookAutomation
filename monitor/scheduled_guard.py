"""Stop scheduled work in old dated mail chats; direct replies remain usable."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from string import Formatter

from .configuration import resolve, zone
from .model import now


class StaleAutomation(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__(result['instruction'])


def _metadata(thread, home):
    """Read only this thread and a bounded local rollout tail."""
    try:
        with closing(sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro',
                                    uri=True, timeout=1)) as db:
            title, path = db.execute('SELECT name, rollout_path FROM threads WHERE id=?',
                                     (thread,)).fetchone()
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell()-2*1024*1024))
            lines = stream.read().decode('utf-8', errors='replace').splitlines()
        trigger, automation_id = None, None
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get('payload', {})
            if not isinstance(payload, dict):
                continue
            if event.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                attribution = payload.get('turn_attribution')
                trigger = attribution.get('turn_trigger') if isinstance(attribution, dict) else None
                automation_id = None
            elif event.get('type') == 'response_item' and payload.get('role') == 'user':
                content = payload.get('content')
                if not isinstance(content, list):
                    continue
                text = '\n'.join(p['text'] for p in content if isinstance(p, dict)
                                 and p.get('type') == 'input_text' and isinstance(p.get('text'), str)).strip()
                if text.startswith('<heartbeat>'):
                    import xml.etree.ElementTree as ET
                    try:
                        automation_id = ET.fromstring(text).findtext('automation_id')
                    except ET.ParseError:
                        pass
        return title, trigger, automation_id
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None, None, None


def check(*, home=None, at=None):
    thread = os.environ.get('CODEX_THREAD_ID')
    if not thread:
        return dict(status='allowed', scheduled=False)
    home = Path(home or os.environ.get('CODEX_HOME', Path.home()/'.codex'))
    title, trigger, automation_id = _metadata(thread, home)
    if not isinstance(trigger, str) or not trigger.startswith('automation_'):
        return dict(status='allowed', scheduled=False, trigger=trigger)
    today = datetime.fromisoformat(at or now()).astimezone(zone()).date()
    chat_day = None
    try:
        pattern = ''.join(literal + (spec if field == 'date' else '')
                          for literal, field, spec, _ in Formatter().parse(resolve().get('schedule.title')))
        chat_day = datetime.strptime(title, pattern).date()
    except (TypeError, ValueError):
        pass
    if chat_day == today:
        return dict(status='allowed', scheduled=True, chat_date=str(chat_day), date=str(today))
    return dict(status='blocked', stop=True, scheduled=True, automation_id=automation_id,
                chat_date=str(chat_day) if chat_day else None, date=str(today),
                instruction='Wrong day: stale automation. Stop and cancel this automation.'
                if chat_day else 'Cannot verify the mail chat date. Stop and cancel this automation.',
                cleanup='Delete only the triggering automation through the app tool. Do not run mail checks, '
                        'restart Outlook, send, write calendars, or create a replacement wake-up. '
                        'Direct user replies in this chat may continue normally.')


def enforce():
    result = check()
    if result['status'] == 'blocked':
        raise StaleAutomation(result)
