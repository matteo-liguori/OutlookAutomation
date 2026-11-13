"""Exercise the installed harness, without mail/calendar backend calls."""

import json
import os
from pathlib import Path
import subprocess
import sys


COMMANDS = (
    ('config', 'validate'),
    ('help', 'retention'),
    ('outlook', 'status'),
    ('briefing', '--task', 'installation-validation'),
    ('doctor',),
)


def run_smoke(root, python=sys.executable, env=None):
    checks = []
    for command in COMMANDS:
        cursor = None
        pages = 0
        try:
            while True:
                args = [python, '-X', 'utf8', '-m', 'monitor', '--format', 'json']
                if cursor:
                    args += ['--cursor', cursor]
                result = subprocess.run(args + list(command), cwd=root, env=env,
                                        capture_output=True, text=True,
                                        encoding='utf-8', timeout=60)
                if result.returncode:
                    try:
                        detail = json.loads(result.stderr or result.stdout).get('error')
                    except (ValueError, AttributeError):
                        detail = None
                    raise ValueError(detail or f'Command exited {result.returncode}')
                data = json.loads(result.stdout)
                rows = data.get('items', [data])
                if data.get('error') or any(r.get('ok') is False for r in rows):
                    raise ValueError('Command reported an error or failed health check')
                pages += 1
                next_cursor = data.get('next_cursor')
                if not next_cursor:
                    break
                if pages >= 100 or next_cursor == cursor:
                    raise ValueError('Paging did not complete within the bounded check')
                cursor = next_cursor
            checks.append(dict(command=list(command), ok=True, pages=pages))
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            checks.append(dict(command=list(command), ok=False, error=str(exc)[:400]))
            return dict(ok=False, checks=checks)
    return dict(ok=True, checks=checks)


def main():
    if os.environ.get('OUTLOOK_AUTOMATION_PROFILE') or os.environ.get('OUTLOOK_AUTOMATION_CUSTOMISATIONS'):
        result = dict(ok=False, error='Remove synthetic profile/configuration overrides before installed validation')
    else:
        result = run_smoke(Path(__file__).resolve().parents[1])
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
