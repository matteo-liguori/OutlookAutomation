"""`chamberlain import <old data root>`: one shot, read-only on the source, idempotent.

Order: read the old installation (nothing is written), move secrets, write the
connection profile if there is none, then every database row in ONE transaction. A second
run finds every derived handle already there and writes nothing.
"""

import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from chamberlain.app.context import Services
from chamberlain.connections.build import build_router, policy_of
from chamberlain.connections.load import dump_connections, load_connections, profile_path
from chamberlain.connections.model import GoogleCalendarSource
from chamberlain.connections.repo import sync_sources
from chamberlain.connectors.caldav.setup import Check, connect_icloud
from chamberlain.connectors.calendar_support.secrets import SecretStore, default_store
from chamberlain.connectors.google_calendar import auth
from chamberlain.importer import authority, instructions, records
from chamberlain.importer.credentials import icloud_credential, move_secrets
from chamberlain.importer.old import SKIPPED_TABLES, OldInstallation, read_installation
from chamberlain.importer.profile_map import MappedProfile, map_profile
from chamberlain.importer.report import ImportReport
from chamberlain.importer.report_template import write_template
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.ports.mail import MailPort
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import open_store, stamp
from chamberlain.store.schema import get_state, set_state
from chamberlain.writes import read_state
from chamberlain.writes.audit import AuditChain
from chamberlain.writes.models import WritePolicy
from chamberlain.writes.seed import DEFAULT_MONTHS, seed_all

STATE_KEY = "importer.last_import"
REPORT_RELATIVE = Path("import") / "report.json"

NEXT_STEPS = (
    "Reinstall the Thunderbird add-on once (python -m chamberlain.connectors.thunderbird.setup).",
    "Open the viewer and confirm each standing grant again; none is active until you do.",
    "Read config/owner-instructions.yaml: your own rules, now given to the agent. Delete any "
    "topic that only describes the old commands.",
    "Keep the old data folder untouched as the fallback; run only one system with write access.",
)


def _guard_paths(old_root: Path, data_root: Path) -> None:
    old, new = old_root.resolve(), data_root.resolve()
    if old == new or old in new.parents or new in old.parents:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "The new data root and the old one must be separate folders.",
            fix="Choose a new, empty data root for Chamberlain.",
        )


def _write_profile(data_root: Path, mapped: MappedProfile, report: ImportReport) -> None:
    path = profile_path(data_root)
    text = dump_connections(mapped.connections)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(".tmp")
        pending.write_text(text, encoding="utf-8")
        pending.replace(path)
        report.config_written = "created"
    elif path.read_text(encoding="utf-8-sig") == text:
        report.config_written = "already identical"
    else:
        report.config_written = "kept existing (differs from the old profile)"
        report.warnings.append("config/connections.yaml already exists and was not changed.")
        _add_read_state(path, mapped, report)


def _add_read_state(path: Path, mapped: MappedProfile, report: ImportReport) -> None:
    """An older profile has no `read_state` section: add it (only when an option is on)."""
    state = mapped.connections.read_state
    existing = path.read_text(encoding="utf-8-sig")
    if not (state.filing_marks_read or state.reporting_marks_read):
        return
    if re.search(r"^read_state:", existing, re.MULTILINE):
        return
    block = "read_state:\n" + "".join(
        f"  {name}: {'true' if value else 'false'}\n"
        for name, value in sorted(state.model_dump().items())
    )
    separator = "" if existing.endswith("\n") else "\n"
    pending = path.with_suffix(".tmp")
    pending.write_text(existing + separator + block, encoding="utf-8", newline="\n")
    pending.replace(path)
    report.count("read_state_options", new=True)


def _check_google_accounts(mapped: MappedProfile, store: SecretStore, report: ImportReport) -> None:
    linked = set(auth.accounts(store))
    for source_id, source in sorted(mapped.connections.sources.items()):
        if isinstance(source, GoogleCalendarSource) and source.account.lower() not in linked:
            report.warnings.append(
                f"{source_id}: no Google sign-in is saved for {source.account}; "
                "run the Google calendar setup (client, then link)."
            )


def _import_all(
    old: OldInstallation,
    old_root: Path,
    data_root: Path,
    the_clock: Clock,
    secrets: SecretStore | None,
    report: ImportReport,
    caldav_check: Check | None = None,
) -> MappedProfile:
    icloud = icloud_credential(old_root)
    mapped = map_profile(old, report, icloud[0] if icloud else None)
    zone = ZoneInfo(mapped.connections.timezone)

    store = secrets or default_store(data_root)
    move_secrets(old_root, store, report, icloud)
    _check_google_accounts(mapped, store, report)
    _write_profile(data_root, mapped, report)
    if icloud is not None and caldav_check is not None:
        _connect_icloud(data_root, store, icloud[0], caldav_check, report)
    write_template(old, data_root, mapped.connections.timezone, report)
    _write_rows(old, mapped, zone, data_root, the_clock, report)
    return mapped


def _connect_icloud(
    data_root: Path, store: SecretStore, username: str, check: Check, report: ImportReport
) -> None:
    """Sign in to iCloud with the moved password and connect its calendars; never fails."""
    try:
        connected = connect_icloud(profile_path(data_root), store, username, check=check)
    except (ChamberlainError, OSError) as error:
        report.warnings.append(
            f"iCloud was not connected ({error}). Once you are online, run "
            '"python -m chamberlain.connectors.caldav.setup import-old --old-data <old folder>".'
        )
        return
    for sid, name in connected:
        report.sources[sid] = f"iCloud calendar {name or '(unnamed)'} (connected)"


def _seed_recipients(
    mapped: MappedProfile,
    data_root: Path,
    the_clock: Clock,
    secrets: SecretStore | None,
    mail: Mapping[str, MailPort] | None,
    report: ImportReport,
) -> None:
    """Read each mail source's Sent folder for known recipients, when the connector is there.

    Never fails the import: a source that cannot be read is reported with the command to
    run later (`chamberlain recipients seed`).
    """
    ports: Mapping[str, MailPort]
    if mail is not None:
        ports = mail
    else:
        try:
            ports = build_router(mapped.connections, data_root, the_clock, secrets=secrets).mail
        except ChamberlainError as error:
            report.warnings.append(f"recipients not seeded: {error.message}")
            return
    if not ports:
        return
    db = open_store(data_root, the_clock)
    try:
        with transaction(db):
            sync_sources(db, mapped.connections, stamp(the_clock))
        for result in seed_all(db, dict(ports), the_clock, months=DEFAULT_MONTHS):
            report.recipients[result.source] = result.summary()
            if not result.complete:
                report.warnings.append(
                    f"{result.source}: recipients not fully seeded ({result.reason}); "
                    "run `chamberlain recipients seed` once the mail client is open."
                )
    finally:
        db.close()


def run_import(
    old_root: Path,
    data_root: Path,
    *,
    clock: Clock | None = None,
    secrets: SecretStore | None = None,
    mail: Mapping[str, MailPort] | None = None,
    seed_recipients: bool = True,
    caldav_check: Check | None = None,
) -> ImportReport:
    """Import an old installation. `caldav_check` signs in to iCloud (the CLI passes the
    real one); without it the iCloud calendar stays a disabled placeholder."""
    the_clock = clock or SystemClock()
    _guard_paths(old_root, data_root)
    old = read_installation(old_root)
    report = ImportReport()
    report.left_behind = dict(old.skipped_rows)
    for table in SKIPPED_TABLES:
        report.left_behind.setdefault(table, 0)
    report.left_behind = {t: n for t, n in report.left_behind.items() if n}
    report.next_steps = list(NEXT_STEPS)

    try:
        mapped = _import_all(old, old_root, data_root, the_clock, secrets, report, caldav_check)
    except ChamberlainError:
        raise
    except (sqlite3.Error, ValidationError, ValueError, KeyError, TypeError) as error:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            f"The import stopped at {report.where or 'the old profile'}; nothing was imported "
            f"from the database ({type(error).__name__}).",
            fix="Fix or remove that record in the copy of the old folder, then import again.",
            detail=str(error)[:300],
        ) from None

    if seed_recipients:
        _seed_recipients(mapped, data_root, the_clock, secrets, mail, report)
    _write_report(data_root, report)
    return report


def _propose_read_grants(
    db: sqlite3.Connection,
    mapped: MappedProfile,
    the_clock: Clock,
    audit: AuditChain,
    report: ImportReport,
    policy: WritePolicy,
) -> None:
    """The owner's v1 read-state options are on: propose the standing grant they need, per
    mailbox, once. Never confirmed here: only a click in the viewer activates it (G-24)."""
    state = mapped.connections.read_state
    if not (state.filing_marks_read or state.reporting_marks_read):
        return
    mailboxes = sorted(s for s in mapped.connections.sources if s.startswith("mail:"))
    for handle in read_state.propose_missing(db, the_clock, audit, mailboxes, policy=policy):
        report.count("read_state_grants", new=True)
        report.read_state.setdefault("grants_proposed", []).append(handle)
    report.read_state["note"] = (
        "Marking mail read needs your click on the proposed standing grant for each mailbox "
        "(viewer, Permissions). Filing that marks mail read also needs the move itself to be "
        "allowed as usual."
    )


def _write_report(data_root: Path, report: ImportReport) -> None:
    target = data_root / REPORT_RELATIVE
    document = json.dumps(report.document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if report.changed or not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            number = 1
            while target.with_name(f"report.{number}.json").exists():
                number += 1
            target.replace(target.with_name(f"report.{number}.json"))
        target.write_text(document, encoding="utf-8", newline="\n")


def _write_rows(
    old: OldInstallation,
    mapped: MappedProfile,
    zone: ZoneInfo,
    data_root: Path,
    the_clock: Clock,
    report: ImportReport,
) -> None:
    db = open_store(data_root, the_clock)
    try:
        now = stamp(the_clock)
        policy = policy_of(load_connections(data_root))
        services = Services.bind(db, the_clock, data_root, policy=policy)
        with transaction(db):
            artifacts = ArtifactService(db, the_clock, data_root)
            imported = records.import_cards(db, old, zone, now, report, artifacts)
            ctx = authority.Context(
                db=db,
                audit=services.audit,
                old=old,
                mapped=mapped,
                zone=zone,
                now=now,
                report=report,
                imported=imported,
            )
            grants = authority.import_grants(ctx)
            authority.import_journals(ctx, grants)
            instructions.carry(db, old, data_root, now, report)
            _propose_read_grants(db, mapped, the_clock, services.audit, report, policy)
            if report.changed:
                counts = json.dumps(report.counts, sort_keys=True)
                services.audit.append(
                    actor=authority.ACTOR,
                    kind="import",
                    subject=None,
                    summary="Imported the old installation: " + counts,
                )
                if get_state(db, STATE_KEY) != counts:
                    set_state(db, the_clock, STATE_KEY, counts)
    finally:
        db.close()
