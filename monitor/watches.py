"""Declarative keyword watches inject relevant model cues, without external effects."""
import json
import re
from .model import dumps


def all_watches(h):
    return [json.loads(r[0]) for r in h.db.execute(
        "SELECT details FROM context WHERE json_extract(details,'$.kind')='keyword-watch' ORDER BY id")]


def put(h, payload):
    if set(payload) - {'id', 'terms', 'text', 'evidence_ids', 'enabled', 'provenance'}:
        raise ValueError('Watches contain declarative terms and a cue only')
    terms = payload.get('terms')
    if not isinstance(terms, list) or not 1 <= len(terms) <= 32 or any(
            not isinstance(t, str) or not t.strip() or len(t) > 100 for t in terms):
        raise ValueError('Watch terms must contain 1..32 short nonempty strings')
    if type(payload.get('enabled', True)) is not bool:
        raise ValueError('Watch enabled must be boolean')
    if not payload.get('evidence_ids'):
        raise ValueError('Watches need user authority evidence')
    prior = h.db.execute('SELECT details FROM context WHERE id=?', (payload['id'],)).fetchone()
    if prior and json.loads(prior[0]).get('kind') != 'keyword-watch':
        raise ValueError('Watch ID belongs to another context record')
    return h.put_record('context', dict(payload, kind='keyword-watch', entity='keyword-watch'))


def cues(h, values, once_per_chat=True):
    """Inspect only this operation's data; retain no encountered content or match logs."""
    text = dumps(values)
    prompts = []
    for rule in all_watches(h):
        if not rule.get('enabled', True):
            continue
        matched = [t for t in rule['terms'] if re.search(r'(?<!\w)'+re.escape(t)+r'(?!\w)', text, re.IGNORECASE)]
        if not matched:
            continue
        instruction = rule['text']
        if once_per_chat:
            from .guidance import once
            instruction = once(h, 'keyword-watch:'+rule['id'], instruction)
        if instruction:
            prompts.append(dict(type='watch_prompt', watch_id=rule['id'], matched_terms=matched,
                                instruction=instruction))
    return prompts
