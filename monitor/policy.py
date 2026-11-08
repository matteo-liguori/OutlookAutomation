"""Reusable permission boundaries; individual grants remain exclusively in SQLite."""
from copy import deepcopy

DEFAULTS = {
    'local_records': True,
    'managed_maintenance': True,
    'routine_filing': True,
    'filing_changes_read_state': False,
    'filing_deletes_mail': False,
    'filing_reports_spam': False,
    'filing_unsubscribes': False,
    'sending': 'conversation-or-stored-permission',
    'calendar_writes': 'specific-or-stored-permission',
    'appointment_permission_covers_email_commitments': True,
    'appointment_permission_covers_invitations_rsvps_cancellations_deadlines': False,
    'new_sending_route_requires_approval': True,
    'unchanged_approved_batch_survives_recovery': True,
}


def effective(profile):
    result=deepcopy(DEFAULTS)
    overrides=profile.get('permission_defaults', {})
    if not isinstance(overrides,dict) or set(overrides)-set(DEFAULTS):
        raise ValueError('Unknown permission-default override')
    for key,value in overrides.items():
        if isinstance(DEFAULTS[key],bool):
            if type(value) is not bool:raise ValueError('Permission-default override needs a boolean: '+key)
        elif value not in {DEFAULTS[key], 'ask-every-time'}:
            raise ValueError('Unsupported permission-default override: '+key)
        result[key]=value
    return result
