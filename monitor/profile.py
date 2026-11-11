"""Installation identity, kept separately from reusable code and task records."""

import json
import os
import re
import platform as host_platform
from pathlib import Path
from automation_paths import data_root

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVIDERS = {'calendar:work':'outlook', 'calendar:primary':'google', 'calendar:shared':'google', 'calendar:icloud':'icloud'}


def platform_settings(value):
    default_os = 'macos' if host_platform.system() == 'Darwin' else 'windows'
    config = value.get('platform', {'os':default_os,'outlook_backend':'native' if default_os=='macos' else 'com'})
    if not isinstance(config,dict) or (config.get('os'),config.get('outlook_backend')) not in {('windows','com'),('macos','native'),('macos','thunderbird')}:
        raise ValueError('Profile platform must select windows/com, macos/native or macos/thunderbird')
    bindings=config.get('bindings',{})
    if not isinstance(bindings,dict) or set(bindings)-set(value['mail'])-set(value['calendars']):
        raise ValueError('Platform bindings must name configured sources')
    for binding in bindings.values():
        if not isinstance(binding,dict) or set(binding)-{'backend','account_id','selected_inbox_id','calendar_id','draft_folder_id','sent_folder_id','trash_folder_id','mail_source','address_book_ids'}:
            raise ValueError('Unknown platform binding field')
        selected=binding.get('backend',config['outlook_backend'])
        if selected not in ({'native','thunderbird'} if config['os']=='macos' else {'com'}):
            raise ValueError('Invalid source backend for this platform')
        if 'address_book_ids' in binding:
            books=binding['address_book_ids']
            if selected!='thunderbird' or not isinstance(books,list) or any(not isinstance(v,str) or not v for v in books) or len(books)!=len(set(books)):
                raise ValueError('Thunderbird address_book_ids must be a list of unique verified IDs')
        for key in ('selected_inbox_id','calendar_id','draft_folder_id','sent_folder_id','trash_folder_id','account_id'):
            if key not in binding:continue
            if selected=='thunderbird':
                if not isinstance(binding[key],str) or not binding[key].strip() or any(c in binding[key] for c in '\r\n\x00'):
                    raise ValueError('Thunderbird binding IDs must be nonempty strings')
            elif key in {'account_id','sent_folder_id','trash_folder_id'} or not re.fullmatch(r'[0-9]+',str(binding[key])):
                raise ValueError('Native binding IDs must be numeric')
        if 'mail_source' in binding and binding['mail_source'] not in value['mail']:
            raise ValueError('Calendar binding must use a configured mailbox')
    return config


def source_backend(value,source):
    config=platform_settings(value)
    return config.get('bindings',{}).get(source,{}).get('backend',config['outlook_backend'])


def native_bindings(value,mailbox,source=None):
    sources=[key for key,address in value['mail'].items() if address.casefold()==mailbox.casefold()]
    if len(sources)!=1:raise ValueError('Mailbox does not match the private profile')
    bindings=platform_settings(value).get('bindings',{})
    mail=bindings.get(sources[0],{})
    calendar=bindings.get(source if source in value['calendars'] else value.get('roles',{}).get('work_calendar','calendar:work'),{})
    legacy=value.get('outlook_native',{})
    if legacy and len(value['mail'])!=1:raise ValueError('Map legacy native bindings to individual sources before using multiple mailboxes')
    result=dict(legacy)
    for key,target in [('selected_inbox_id','inbox_id'),('draft_folder_id','draft_folder_id')]:
        if key in mail:result[target]=mail[key]
    if 'calendar_id' in calendar:result['calendar_id']=calendar['calendar_id']
    return result


def load(path=None):
    path = Path(path or os.environ.get("OUTLOOK_AUTOMATION_PROFILE") or data_root() / 'monitor/profile.json')
    if not path.is_file():
        raise ValueError("Create your local profile with Initialize-OutlookAutomation.ps1 (Windows) or setup_outlook.py (Mac) before using the harness")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != 1:
        raise ValueError("Unsupported profile schema")
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise ValueError("Profile needs your name")
    from zoneinfo import ZoneInfo
    ZoneInfo(value.get("timezone", "Europe/London"))
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
    platform_settings(value)
    return value


PROFILE = load()
