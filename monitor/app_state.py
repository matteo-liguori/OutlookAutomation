"""Bounded read-only Codex metadata checks; unavailable schemas remain unknown."""
import os
from pathlib import Path
import sqlite3
import tomllib
from contextlib import closing
from .timing import plan, verify


def inspect(thread_id, title, home=None):
    home = Path(home or os.environ.get('CODEX_HOME', Path.home()/'.codex'))
    result = dict(title=None, followups=None, runtime_status='unknown', user_present=None)
    db = home/'state_5.sqlite'
    if db.is_file():
        try:
            with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=1)) as c:
                row = c.execute('SELECT name FROM threads WHERE id=?', (thread_id,)).fetchone()
                if row:
                    result['title'] = row[0] == title
        except (sqlite3.Error, OSError):
            pass
    history = home/'thread_history_1.sqlite'
    if history.is_file():
        try:
            with closing(sqlite3.connect(history.as_uri()+'?mode=ro', uri=True, timeout=1)) as c:
                row = c.execute('SELECT status FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1',
                                (thread_id,)).fetchone()
                if row:
                    result['runtime_status'] = row[0]
        except (sqlite3.Error, OSError):
            pass
    current = plan()
    if not current['eligible']:
        result['followups'] = True
        return result
    folder = home/'automations'
    if not folder.is_dir():
        return result
    paths = list(folder.glob('*/automation.toml'))
    if len(paths) > 256:
        return result
    matches = []
    unreadable = False
    for path in paths:
        try:
            if path.stat().st_size > 65536:
                unreadable = True
                continue
            with path.open('rb') as f:
                a = tomllib.load(f)
            if a.get('kind') == 'heartbeat' and a.get('target_thread_id') == thread_id and a.get('status') == 'ACTIVE':
                try:
                    verify(dict(plan=current, actual_rrule=a.get('rrule','')))
                except (ValueError, KeyError, TypeError):
                    continue
                matches.append(a.get('id'))
        except (OSError, ValueError):
            unreadable = True
    if len(matches) == 1 and not unreadable:
        result['followups'] = True
        result['automation_id'] = matches[0]
    elif len(matches) > 1:
        result['followups'] = False
        result['duplicate_followups'] = True
    elif not unreadable:
        result['followups'] = False
    return result
