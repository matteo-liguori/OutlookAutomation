"""Reusable permission boundaries; individual grants remain exclusively in SQLite."""
from copy import deepcopy

from .configuration import defaults
DEFAULTS = defaults()['permission_defaults']


def effective(profile):
    from .configuration import resolve, _current
    result=deepcopy((resolve() if _current.get() is not None else resolve(profile=profile)).values['permission_defaults'])
    return result
