"""Installation identity, kept separately from reusable code and task records."""

import json
import os
import re
from pathlib import Path
from automation_paths import data_root

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVIDERS = {'calendar:work':'outlook', 'calendar:primary':'google', 'calendar:shared':'google', 'calendar:icloud':'icloud'}


def load(path=None):
    path = Path(path or os.environ.get("OUTLOOK_AUTOMATION_PROFILE") or data_root() / 'monitor/profile.json')
    if not path.is_file():
        raise ValueError("Create your local profile with Initialize-OutlookAutomation.ps1 (Windows) or setup_outlook.py (Mac) before using the harness")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != 1:
        raise ValueError("Unsupported profile schema")
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise ValueError("Profile needs your name")
    if value.get("timezone") != "Europe/London":
        raise ValueError("This release uses Europe/London scheduling")
    mail = value.get("mail")
    if not isinstance(mail, dict) or not mail or any(not re.fullmatch(r'mail:[a-z][a-z0-9_-]*', k) for k in mail):
        raise ValueError("Configure at least one supported mail role")
    for address in mail.values():
        if not isinstance(address, str) or address.count("@") != 1 or any(c.isspace() for c in address):
            raise ValueError("Mail stores must be explicit SMTP addresses")
    if len({s.casefold() for s in mail.values()}) != len(mail):
        raise ValueError("Each mail role needs a distinct store")
    calendars = value.get("calendars")
    if not isinstance(calendars, list) or any(not isinstance(s, str) for s in calendars):
        raise ValueError("Calendars must be a list of supported source names")
    if len(calendars) != len(set(calendars)) or any(not re.fullmatch(r'calendar:[a-z][a-z0-9_-]*', k) for k in calendars):
        raise ValueError("Calendar sources must be supported and unique")
    roles = value.get('roles', {})
    if not isinstance(roles,dict) or not isinstance(value.get('calendar_providers',{}),dict) or not isinstance(value.get('instructions',{}),dict):
        raise ValueError('Profile roles, providers and instructions must be objects')
    work_calendar = roles.get('work_calendar', 'calendar:work')
    work_mail = roles.get('work_mail', 'mail:work')
    if work_calendar in calendars and work_mail not in mail:
        raise ValueError('The work calendar needs a configured work mail store')
    providers = {**DEFAULT_PROVIDERS, **value.get('calendar_providers', {})}
    if any(providers.get(k) not in {'outlook','google','icloud'} for k in calendars):
        raise ValueError('Each calendar source needs a provider')
    if any(not isinstance(v,str) for v in value.get('instructions',{}).values()):
        raise ValueError('Profile instructions must contain text')
    from .policy import effective
    effective(value)
    return value


PROFILE = load()
