"""The old `profile.json` and `customisations.yaml` become the new connection profile.

Bindings carry over verbatim (Thunderbird account and folder ids, calendar ids, Google
account and calendar). A binding that cannot be carried exactly is skipped and reported,
never guessed: a wrong folder id would file or send in the wrong place.
"""

import re
from dataclasses import dataclass, field
from typing import Any, cast

from chamberlain.connections.model import (
    BudgetsConfig,
    CalDavSource,
    Connections,
    GoogleCalendarSource,
    OwlCalendarSource,
    ScheduleConfig,
    SourceConfig,
    ThunderbirdContactsSource,
    ThunderbirdMailSource,
    UpdatesConfig,
    WritePolicyConfig,
)
from chamberlain.connectors.caldav.credentials import credential_name
from chamberlain.domain.grants import Requirement
from chamberlain.domain.read_state import ReadStateConfig
from chamberlain.importer.old import Json, OldInstallation
from chamberlain.importer.report import ImportReport
from chamberlain.schema.errors import ChamberlainError, ErrorCode

ICLOUD_ROOT_URL = "https://caldav.icloud.com/"
DEFAULT_ZONE = "Europe/London"
"""The old harness's default zone, used when neither file names one."""
ASK_EVERY_TIME = "ask-every-time"
_DEFAULT_PROVIDERS = {
    "calendar:work": "outlook",
    "calendar:primary": "google",
    "calendar:shared": "google",
    "calendar:icloud": "icloud",
}
_MAIL_FIELDS = {
    "account_id": "account_id",
    "selected_inbox_id": "inbox_folder_id",
    "sent_folder_id": "sent_folder_id",
    "draft_folder_id": "draft_folder_id",
    "trash_folder_id": "trash_folder_id",
}
_SETTING_GROUPS = ("reviews", "maintenance", "templates")
_CLOCK_TEXT = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")


@dataclass
class MappedProfile:
    connections: Connections
    mail_by_address: dict[str, str] = field(default_factory=dict[str, str])
    """Lower-cased mailbox address to new source id (for placing old operations)."""
    calendar_by_id: dict[str, str] = field(default_factory=dict[str, str])
    """Old Thunderbird or Google `calendar_id` to new source id."""
    default_mail: str | None = None
    default_calendar: str | None = None
    icloud_source: str | None = None
    """The disabled CalDAV placeholder, when the old profile had iCloud."""


def new_source_id(old: str) -> str:
    """Old ids allowed `_`; the new pattern does not."""
    role, _, name = old.partition(":")
    cleaned = re.sub(r"[^a-z0-9-]", "-", name.lower().replace("_", "-"))[:32].strip("-")
    return f"{role}:{cleaned or 'main'}"


def _text(binding: Json, key: str) -> str | None:
    value = binding.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bindings(profile: Json) -> tuple[str, dict[str, Json]]:
    raw = profile.get("platform")
    platform: Json = cast(Json, raw) if isinstance(raw, dict) else {}
    backend = str(platform.get("outlook_backend") or "")
    found = platform.get("bindings")
    bindings = cast(dict[str, Json], found) if isinstance(found, dict) else {}
    return backend, bindings


def _is_thunderbird(binding: Json, default_backend: str) -> bool:
    return str(binding.get("backend") or default_backend) == "thunderbird"


def _zone(old: OldInstallation) -> str:
    custom = old.customisations.get("timezone")
    profile = old.profile.get("timezone")
    for candidate in (custom, profile):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return DEFAULT_ZONE


def _policy(old: OldInstallation, report: ImportReport) -> WritePolicyConfig:
    merged: dict[str, Any] = {}
    for layer in (
        old.profile.get("permission_defaults"),
        old.customisations.get("permission_defaults"),
    ):
        if isinstance(layer, dict):
            merged.update(cast(Json, layer))
    send = Requirement.AGENT_EVIDENCE
    other = Requirement.AGENT_EVIDENCE
    if merged.get("sending") == ASK_EVERY_TIME:
        send = Requirement.HUMAN_CONFIRMATION
    if merged.get("calendar_writes") == ASK_EVERY_TIME:
        other = Requirement.HUMAN_CONFIRMATION
    ignored = sorted(set(merged) - {"sending", "calendar_writes", *READ_SETTINGS})
    if ignored:
        report.unmapped_settings["permission_defaults"] = {k: merged[k] for k in ignored}
    return WritePolicyConfig(send_known=send, other=other)


READ_SETTINGS = ("filing_changes_read_state", "reporting_marks_read")


def _read_state(old: OldInstallation, report: ImportReport) -> ReadStateConfig:
    """The owner's v1 read-state options become the v2 profile options (05, G-24)."""
    merged: dict[str, Any] = {}
    for layer in (
        old.profile.get("permission_defaults"),
        old.customisations.get("permission_defaults"),
    ):
        if isinstance(layer, dict):
            merged.update(cast(Json, layer))
    state = ReadStateConfig(
        filing_marks_read=merged.get("filing_changes_read_state") is True,
        reporting_marks_read=merged.get("reporting_marks_read") is True,
    )
    report.read_state = state.model_dump()
    return state


def _clock_time(value: object) -> str | None:
    """`HH:MM` text or minutes after midnight (840 is 14:00), as the old files wrote them."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value < 24 * 60:
        return f"{value // 60:02d}:{value % 60:02d}"
    if isinstance(value, str):
        found = _CLOCK_TEXT.match(value.strip())
        if found:
            return f"{int(found.group(1)):02d}:{found.group(2)}"
    return None


def _schedules(old: OldInstallation, report: ImportReport) -> tuple[ScheduleConfig, ...]:
    """The morning pass and each continuation become daily `mail` passes."""
    raw = old.customisations.get("schedule")
    if not isinstance(raw, dict):
        return ()
    rest: dict[str, Any] = dict(cast(Json, raw))
    found: dict[str, ScheduleConfig] = {}

    def add(name: str, value: object) -> bool:
        at = _clock_time(value)
        if at is None:
            return False
        found.setdefault(at, ScheduleConfig(name=name, kind="mail", at=at))
        return True

    if add("morning", rest.get("morning")):
        del rest["morning"]
    continuations = rest.get("continuations")
    if isinstance(continuations, list):
        items = cast(list[Any], continuations)
        left = [
            v
            for v in items
            if not add(f"continuation-{(_clock_time(v) or '').replace(':', '')}", v)
        ]
        if left:
            rest["continuations"] = left
        else:
            del rest["continuations"]
    if rest:
        report.unmapped_settings["schedule"] = rest
    return tuple(found.values())


def _updates(old: OldInstallation, report: ImportReport) -> UpdatesConfig:
    raw = old.customisations.get("updates")
    if not isinstance(raw, dict):
        return UpdatesConfig()
    rest: dict[str, Any] = dict(cast(Json, raw))
    enabled = rest.get("enabled")
    remote = rest.get("remote")
    config = UpdatesConfig(
        enabled=enabled if isinstance(enabled, bool) else False,
        remote=remote.strip() if isinstance(remote, str) and remote.strip() else "origin",
    )
    if isinstance(enabled, bool):
        del rest["enabled"]
    if isinstance(remote, str) and remote.strip():
        del rest["remote"]
    if rest:
        report.unmapped_settings["updates"] = rest
    return config


def _carry_settings(old: OldInstallation, report: ImportReport) -> None:
    for group in _SETTING_GROUPS:
        value = old.customisations.get(group)
        if value:
            report.unmapped_settings[group] = value


def map_profile(
    old: OldInstallation, report: ImportReport, icloud_username: str | None = None
) -> MappedProfile:
    profile = old.profile
    backend, bindings = _bindings(profile)
    sources: dict[str, SourceConfig] = {}
    mapped = MappedProfile(connections=Connections())
    origin: dict[str, str] = {}

    def claim(sid: str, name: str) -> None:
        if sid in origin and origin[sid] != name:
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                f"The old sources {origin[sid]} and {name} would both become {sid}.",
                fix="Rename one of them in the old profile copy, then import again.",
            )
        origin[sid] = name

    mail = profile.get("mail")
    for name, address in sorted(cast(Json, mail).items() if isinstance(mail, dict) else []):
        sid = new_source_id(name)
        claim(sid, name)
        report.where = f"source {name}"
        binding = bindings.get(name, {})
        if not isinstance(address, str) or not _is_thunderbird(binding, backend):
            report.skipped_sources.append(
                f"{name}: not bound to Thunderbird (Outlook COM and native bindings are dropped)"
            )
            continue
        values = {new: _text(binding, old_key) for old_key, new in _MAIL_FIELDS.items()}
        missing = sorted(k for k, v in values.items() if v is None)
        if missing:
            report.skipped_sources.append(f"{name}: the old binding lacks {', '.join(missing)}")
            continue
        sources[sid] = ThunderbirdMailSource(
            label=name,
            mailbox=address.strip(),
            account_id=str(values["account_id"]),
            inbox_folder_id=str(values["inbox_folder_id"]),
            sent_folder_id=str(values["sent_folder_id"]),
            draft_folder_id=str(values["draft_folder_id"]),
            trash_folder_id=str(values["trash_folder_id"]),
        )
        mapped.mail_by_address[address.strip().lower()] = sid
        mapped.default_mail = mapped.default_mail or sid
        report.sources[sid] = f"Thunderbird mailbox {address.strip()}"
        books: list[Any] = cast(list[Any], binding.get("address_book_ids") or [])
        names = [b for b in books if isinstance(b, str) and b]
        if names and len(names) == len(books) and len(set(names)) == len(names):
            cid = "contacts:" + sid.partition(":")[2]
            claim(cid, f"{name} (address books)")
            sources[cid] = ThunderbirdContactsSource(label=name, address_book_ids=tuple(names))
            report.sources[cid] = f"Thunderbird address books ({len(names)})"

    providers = {**_DEFAULT_PROVIDERS}
    custom_providers = profile.get("calendar_providers")
    if isinstance(custom_providers, dict):
        providers.update(cast(dict[str, str], custom_providers))
    calendars = profile.get("calendars")
    for name in cast(list[str], calendars) if isinstance(calendars, list) else []:
        sid = new_source_id(name)
        claim(sid, name)
        report.where = f"source {name}"
        binding = bindings.get(name, {})
        provider = providers.get(name)
        calendar_id = _text(binding, "calendar_id")
        if provider == "icloud":
            sources[sid] = CalDavSource(
                label=name,
                enabled=False,
                url=ICLOUD_ROOT_URL,
                username=icloud_username or "pending",
                credential=credential_name(icloud_username) if icloud_username else "pending",
            )
            mapped.icloud_source = sid
            report.sources[sid] = "iCloud calendar (disabled until `caldav.setup register`)"
            continue
        if not _is_thunderbird(binding, backend) or calendar_id is None:
            report.skipped_sources.append(
                f"{name}: no Thunderbird or Google calendar binding to carry over"
            )
            continue
        if binding.get("transport") == "google":
            account, _, google_calendar = calendar_id.partition("/")
            if not account or not google_calendar:
                report.skipped_sources.append(
                    f"{name}: the Google calendar id is not <account>/<id>"
                )
                continue
            sources[sid] = GoogleCalendarSource(
                label=name, account=account, calendar_id=google_calendar
            )
            report.sources[sid] = f"Google calendar {google_calendar} of {account}"
        else:
            sources[sid] = OwlCalendarSource(label=name, calendar_id=calendar_id)
            report.sources[sid] = "Thunderbird (Owl) calendar"
        mapped.calendar_by_id[calendar_id] = sid
        mapped.default_calendar = mapped.default_calendar or sid

    _carry_settings(old, report)
    mapped.connections = Connections(
        timezone=_zone(old),
        write_policy=_policy(old, report),
        read_state=_read_state(old, report),
        budgets=BudgetsConfig(),
        schedules=_schedules(old, report),
        updates=_updates(old, report),
        sources=sources,
    )
    return mapped
