"""Operation-specific instructions, loaded only by relevant actions or triggers."""

RELEVANCE_REVIEW = (
    'At review, judge ongoing relevance from the date, context and risk of error. '
    'Close unless a concrete current reason warrants tracking; technical incompleteness alone does not. '
    'Use judgment in clear cases. Ask if genuinely unsure or missing a material fact you cannot reasonably retrieve. '
    'Record why; email evidence can be retrieved again.'
)

from .configuration import defaults, resolve
GUIDES = defaults()['instructions']


def guide(topic):
    if topic not in GUIDES:
        raise ValueError("Unknown guidance topic")
    from .profile import PROFILE
    instruction = resolve().instruction(topic)
    result = {"topic": topic, "instruction": instruction}
    if topic in {'permissions','calendar-decisions','filing'}:
        from .policy import effective
        result['effective_permission_defaults'] = effective(PROFILE)
        result['permission_source'] = 'Individual grants and approval scope are in SQLite; profile overrides affect default boundaries only.'
    if topic == 'accounts':
        result['profile'] = {k:PROFILE[k] for k in ('name','timezone','mail','calendars','roles','calendar_providers','paths') if k in PROFILE}
        from automation_paths import data_root
        result['data_root'] = str(data_root())
    if topic == 'outlook':
        from outlook_platforms.api import status
        result.update(status())
    return result


COMMAND_TOPICS = {
    'case edit': 'card-input', 'case note-ack': 'card-input',
    'briefing': 'briefing',
    'case': 'card-upkeep', 'review': 'card-upkeep',
    'outlook': 'outlook',
    'case update': 'card-writes', 'case merge': 'case-organisation',
    'case split': 'case-organisation', 'action review': 'action-review',
    'approval': 'permissions', 'run': 'pass-receipts', 'report': 'pass-receipts',
    'filing': 'filing', 'schedule': 'scheduling', 'thread': 'thread-cache',
    'maintenance': 'retention', 'backup': 'retention', 'calendar': 'calendar-decisions',
    'refresh': 'mail-access', 'health': 'mail-access',
    'evidence': 'evidence', 'context': 'context',
}


def help_for(words):
    if not words:
        return {'usage': 'monitor.ps1 help <topic or command>',
                'topics': [{'topic': k, 'summary': v.splitlines()[0].lstrip('# ')}
                           for k, v in sorted(GUIDES.items())],
                'examples': ['help case update', 'help action review', 'help filing']}
    if words[0]=='outlook' and len(words)>1:
        from outlook_platforms.api import available_operations
        if words[1] not in available_operations():raise ValueError('Unknown Outlook operation; use help outlook')
    target = ' '.join(words)
    topic = target if target in GUIDES else COMMAND_TOPICS.get(target)
    if topic is None:
        topic = COMMAND_TOPICS.get(words[0])
    if topic is None:
        raise ValueError('Unknown instruction topic or command; use monitor.ps1 help')
    return guide(topic)


def context_prompts(items):
    prompts = []
    gaps = [i for i in items if i.get('type') == 'source_health'
            and i.get('source', '').startswith('mail:') and i.get('status') == 'partial'
            and i.get('coverage_gap_from')]
    if gaps:
        prompts.append(dict(type='mail_coverage_prompt', **guide('mail-coverage')))
    if any(i not in gaps and i.get('type') == 'source_health'
           and (i.get('source', '').startswith('mail:') or i.get('source') == 'outlook:refresh')
           and i.get('status') != 'current' for i in items):
        prompts.append(dict(type='mail_access_prompt', **guide('mail-access')))
    if any(i.get('type') == 'approval_review' and i.get('review_due') for i in items):
        prompts.append(dict(type='permission_review_prompt', **guide('permissions')))
    if any(any(key in i for key in ('backup_rotation_warning', 'thread_cache_warning', 'maintenance_warning'))
           for i in items):
        prompts.append(dict(type='maintenance_prompt', **guide('retention')))
    return prompts
