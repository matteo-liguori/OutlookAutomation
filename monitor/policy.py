"""Reusable permission boundaries; individual grants remain exclusively in SQLite."""
from copy import deepcopy

from .configuration import defaults
DEFAULTS = defaults()['permission_defaults']


def effective(profile):
    from .configuration import resolve, _current
    result=deepcopy((resolve() if _current.get() is not None else resolve(profile=profile)).values['permission_defaults'])
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
