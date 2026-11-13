"""Advisory chat-date checks with a persistent half-hour notification cooldown."""
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from string import Formatter

from .configuration import resolve, zone
from .model import now
from .paths import data_root


def _metadata(thread, home):
    """Read only this thread and a bounded local rollout tail."""
    try:
        with closing(sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro',
                                    uri=True, timeout=1)) as db:
            title, path = db.execute('SELECT name, rollout_path FROM threads WHERE id=?',
                                     (thread,)).fetchone()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None, None, None
    try:
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
        return title, None, None


def check(*, home=None, at=None):
    thread = os.environ.get('CODEX_THREAD_ID')
    if not thread:
        return dict(status='allowed', scheduled=False)
    home = Path(home or os.environ.get('CODEX_HOME', Path.home()/'.codex'))
    title, trigger, automation_id = _metadata(thread, home)
    scheduled = isinstance(trigger, str) and trigger.startswith('automation_')
    today = datetime.fromisoformat(at or now()).astimezone(zone()).date()
    chat_day = None
    try:
        pattern = ''.join(literal + (spec if field == 'date' else '')
                          for literal, field, spec, _ in Formatter().parse(resolve().get('schedule.title')))
        chat_day = datetime.strptime(title, pattern).date()
    except (TypeError, ValueError):
        pass
    if chat_day is None or chat_day == today:
        return dict(status='allowed', scheduled=scheduled, chat_date=str(chat_day) if chat_day else None,
                    date=str(today))
    return dict(status='warning', scheduled=scheduled, automation_id=automation_id,
                chat_date=str(chat_day), date=str(today),
                instruction="This chat might be stale. Continue if you're interacting live with the user; "
                            'otherwise check whether this is a stale automation. If the run is stale or '
                            'obsolete, it is okay to stop without finishing its original task. You do not '
                            'need to complete obsolete work just to be thorough or helpful.')


def notice():
    """Claim one notification per chat per 30 minutes, across processes/adapters.

    Retain only hashed chat identifiers and expiry timestamps in private SQLite.
    Expired entries are pruned on the next warning; no timer or model wake is used.
    """
    result = check()
    if result['status'] != 'warning':
        return result
    stamp = datetime.fromisoformat(now()).timestamp()
    key = hashlib.sha256(os.environ['CODEX_THREAD_ID'].encode()).hexdigest()
    path = data_root()/'monitor/chat-warning.sqlite'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS cooldown (chat TEXT PRIMARY KEY, expires REAL NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM cooldown WHERE expires<=?', (stamp,))
            if db.execute('SELECT 1 FROM cooldown WHERE chat=?', (key,)).fetchone():
                return dict(status='allowed', warning_suppressed=True)
            db.execute('INSERT INTO cooldown VALUES (?,?)', (key, stamp+1800))
    except (OSError, sqlite3.Error):
        result['cooldown_unavailable'] = True
    return result


def emit_warning():
    result = notice()
    if result['status'] == 'warning':
        print(json.dumps(dict(chat_date_warning=result), ensure_ascii=False), file=sys.stderr)
