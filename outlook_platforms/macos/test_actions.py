"""Execute one ledger-backed, explicitly authorized native self-test action."""
import json
import hashlib
from pathlib import Path
import re
import subprocess

from .native import SCRIPT

ACTIONS = SCRIPT.with_name('test_actions.js')


def audit_result(result):
    """Keep collected mail bodies out of generic harness evidence and ledgers."""
    result = json.loads(json.dumps(result))
    for item in result.get('items', []):
        if 'body' in item:
            body = item.pop('body')
            item.update(body_length=len(body), body_sha256=hashlib.sha256(body.encode()).hexdigest())
    return result


def execute_action(request, runner=subprocess.run):
    if not re.fullmatch('[a-f0-9]{12}', request.get('token', '')):
        raise ValueError('A unique test ledger token is required')
    if request.get('authorized') is not True:
        raise ValueError('Record explicit test authorization before running mutations')
    source = SCRIPT.read_text() + '\n' + ACTIONS.read_text()
    try:
        p = runner(['/usr/bin/osascript', '-l', 'JavaScript', '-e', source, json.dumps(request)],
                   capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return dict(status='blocked', code='test-action-timeout', mutation_may_have_occurred=True)
    if p.returncode:
        return dict(status='blocked', code='test-action-transport-failed', mutation_may_have_occurred=True)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return dict(status='blocked', code='invalid-test-action-response', mutation_may_have_occurred=True)
