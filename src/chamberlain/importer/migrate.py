"""Resumable v1 migration using a preserved snapshot and read-only comparison.

The operator may select Windows scheduled tasks explicitly; the JSON adapter is confined
to an isolated sandbox. Neither discovers tasks nor performs a switch without a request.
"""

import base64
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml

from chamberlain.attention.owner_rules import path_of
from chamberlain.connections.load import load_connections
from chamberlain.connectors.calendar_support.secrets import SecretStore
from chamberlain.importer.authority import DEAD_STATUSES
from chamberlain.importer.convert import derive, is_sha256, sha256_text
from chamberlain.importer.credentials import (
    GOOGLE_CLIENT,
    GOOGLE_TOKENS,
    ICLOUD_FILE,
    ICLOUD_WINDOWS_FILE,
)
from chamberlain.importer.instructions import collect
from chamberlain.importer.old import (
    CUSTOMISATIONS,
    DB_RELATIVE,
    PROFILE_RELATIVE,
    read_installation,
)
from chamberlain.importer.profile_map import map_profile
from chamberlain.importer.records import OPEN_ACTION, card_rules
from chamberlain.importer.report import ImportReport
from chamberlain.importer.run import run_import
from chamberlain.ports.clock import Clock
from chamberlain.store.db import DB_NAME

_LOCK: Any = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")


@dataclass(frozen=True)
class Verification:
    matched_cards: int
    matched_actions: int
    differences: tuple[str, ...]
    coverage: str = (
        "Local cards, actions, instructions, source bindings and imported authority; "
        "optional offline report bindings. No credential validation or remote mail/calendar "
        "comparison; "
        "backup includes only importer inputs and omits unrelated files and external "
        "credential locations."
    )

    @property
    def passed(self) -> bool:
        return not self.differences


@dataclass(frozen=True)
class MigrationStatus:
    stage: str
    backup: Path
    verification: Verification | None = None


def _digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(
                "The old installation contains a symlink; copy it without links first."
            )
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def _inputs(root: Path) -> list[Path]:
    fixed = [
        DB_RELATIVE,
        PROFILE_RELATIVE,
        Path(CUSTOMISATIONS),
        Path(".state/defaults.json"),
        GOOGLE_CLIENT,
        Path(ICLOUD_FILE),
        Path(ICLOUD_WINDOWS_FILE),
    ]
    token_dir = root / GOOGLE_TOKENS
    _safe_path(root, token_dir)
    tokens = list(token_dir.glob("*.json")) if token_dir.is_dir() else []
    result: list[Path] = []
    for path in [*(root / item for item in fixed), *tokens]:
        _safe_path(root, path)
        if path.is_file():
            result.append(path.relative_to(root))
    return sorted(result)


def _safe_path(root: Path, path: Path) -> None:
    for candidate in [root, *(root / part for part in path.relative_to(root).parents), path]:
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if (
                candidate.is_symlink()
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("Migration inputs cannot include symlinks or reparse points.")


def _input_digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in _inputs(root):
        with (root / relative).open("rb") as stream:
            result[relative.as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def _copy_inputs(root: Path, target: Path) -> None:
    target.mkdir()
    for relative in _inputs(root):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, path, follow_symlinks=False)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temp.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    os.replace(temp, path)


@contextmanager
def _lock(root: Path) -> Generator[None]:
    folder = root / "import"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "migration.lock").open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                _LOCK.locking(stream.fileno(), _LOCK.LK_NBLCK, 1)
            else:
                _LOCK.flock(stream.fileno(), _LOCK.LOCK_EX | _LOCK.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another migration is running.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                _LOCK.locking(stream.fileno(), _LOCK.LK_UNLCK, 1)
            else:
                _LOCK.flock(stream.fileno(), _LOCK.LOCK_UN)


def _guard_paths(old_root: Path, data_root: Path) -> None:
    old, new = old_root.resolve(), data_root.resolve()
    if old == new or old in new.parents or new in old.parents:
        raise ValueError("Old and new data roots must be separate folders.")


def _paths(data_root: Path) -> tuple[Path, Path]:
    return data_root / "import" / "migration.json", data_root.parent / (
        data_root.name + ".v1-backup"
    )


def _load(old_root: Path, data_root: Path) -> dict[str, Any]:
    state_path, backup = _paths(data_root)
    state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
    if state["old_root"] != str(old_root.resolve()) or not backup.is_dir():
        raise ValueError("Migration source or backup does not match the saved migration.")
    if _digest(backup) != state["backup_digest"]:
        raise ValueError("The preserved v1 backup has changed.")
    return state


def detect_migration(old_root: Path) -> dict[str, int | bool]:
    """Inspect v1 without running it or disclosing names, credentials, or record contents."""
    if not (old_root / DB_RELATIVE).is_file() or not (old_root / PROFILE_RELATIVE).is_file():
        return {"detected": False}
    old = read_installation(old_root)
    return {
        "detected": True,
        "schema_version": old.schema_version,
        "cases": len(old.cases),
        "actions": len(old.actions),
        "approvals": len(old.approvals),
    }


def _saved_verification(state: dict[str, Any]) -> Verification | None:
    value = state.get("verification")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Saved migration verification is invalid.")
    fields = cast(dict[str, Any], value)
    differences = fields.get("differences")
    if (
        not isinstance(fields.get("matched_cards"), int)
        or not isinstance(fields.get("matched_actions"), int)
        or not isinstance(differences, list)
        or not all(isinstance(item, str) for item in cast(list[Any], differences))
    ):
        raise ValueError("Saved migration verification is invalid.")
    return Verification(
        matched_cards=fields["matched_cards"],
        matched_actions=fields["matched_actions"],
        differences=tuple(cast(list[str], differences)),
    )


def migration_status(old_root: Path, data_root: Path) -> MigrationStatus:
    """Read saved progress without creating a data root, lock, or database."""
    _guard_paths(old_root, data_root)
    state_path, backup = _paths(data_root)
    if not state_path.is_file():
        return MigrationStatus("not_started", backup)
    state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("old_root") != str(old_root.resolve()):
        raise ValueError("Migration source does not match the saved migration.")
    if state.get("stage") != "copying":
        state = _load(old_root, data_root)
    return MigrationStatus(str(state["stage"]), backup, _saved_verification(state))


def prepare_migration(
    old_root: Path,
    data_root: Path,
    *,
    clock: Clock | None = None,
    secrets: SecretStore | None = None,
) -> MigrationStatus:
    """Detect, preserve and import v1; safe to resume after interrupted import."""
    _guard_paths(old_root, data_root)
    read_installation(old_root)
    state_path, backup = _paths(data_root)
    with _lock(data_root):
        if state_path.exists():
            state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("old_root") != str(old_root.resolve()):
                raise ValueError("Migration source does not match the saved migration.")
        else:
            if backup.exists():
                raise ValueError("Unrecorded backup already exists; inspect it before proceeding.")
            _guard_paths(old_root, backup)
            state = {
                "old_root": str(old_root.resolve()),
                "backup_digest": _input_digest(old_root),
                "stage": "copying",
            }
            _atomic_json(state_path, state)
        if state["stage"] == "copying":
            before = state["backup_digest"]
            if backup.exists():
                if _digest(backup) != before:
                    raise ValueError("The interrupted backup differs from the recorded source.")
            else:
                if _input_digest(old_root) != before:
                    raise ValueError(
                        "The source changed since backup began; inspect before retrying."
                    )
                temporary = backup.with_name("v1-backup-" + uuid4().hex + ".tmp")
                _copy_inputs(old_root, temporary)
                if _input_digest(old_root) != before or _digest(temporary) != before:
                    raise ValueError(
                        "The old installation changed during backup; inspect before retrying."
                    )
                read_installation(temporary)
                os.replace(temporary, backup)
            state["stage"] = "backed_up"
            _atomic_json(state_path, state)
        else:
            state = _load(old_root, data_root)
        if state["stage"] == "backed_up":
            run_import(
                backup,
                data_root,
                clock=clock,
                secrets=secrets,
                seed_recipients=False,
                allow_external_credential=False,
            )
            state["stage"] = "imported"
            _atomic_json(state_path, state)
        return MigrationStatus(stage=str(state["stage"]), backup=backup)


def _verify(old_root: Path, data_root: Path, report_snapshot: Path | None = None) -> Verification:
    old = read_installation(old_root)
    differences: list[str] = []
    instructions = collect(old, ImportReport())
    instruction_path = path_of(data_root)
    document: Any = (
        yaml.safe_load(instruction_path.read_text(encoding="utf-8"))
        if instruction_path.exists()
        else {}
    )
    topics: Any = (
        cast(dict[str, Any], document).get("topics", {}) if isinstance(document, dict) else {}
    )
    for topic, expected in instructions.items():
        actual: Any = cast(dict[str, Any], topics).get(topic) if isinstance(topics, dict) else None
        if (
            not isinstance(actual, dict)
            or cast(dict[str, Any], actual).get("text") != expected
            or not cast(dict[str, Any], actual).get("where")
        ):
            differences.append(f"owner instruction {topic}: missing, changed or disabled")
    actual_sources = load_connections(data_root).sources
    icloud = actual_sources.get("calendar:icloud")
    username = getattr(icloud, "username", None)
    expected_sources = map_profile(old, ImportReport(), username).connections.sources
    for source, expected in expected_sources.items():
        actual = actual_sources.get(source)
        if actual is None or actual.model_dump(mode="json") != expected.model_dump(mode="json"):
            differences.append(f"source {source}: missing or binding differs")
    if report_snapshot is not None:
        if report_snapshot.stat().st_size > 262144:
            raise ValueError("Source report snapshot exceeds 256 KiB.")
        report: Any = json.loads(report_snapshot.read_text(encoding="utf-8"))
        sources: Any = (
            cast(dict[str, Any], report).get("sources") if isinstance(report, dict) else None
        )
        if not isinstance(sources, dict) or len(cast(dict[str, Any], sources)) > 1000:
            raise ValueError(
                "Source report must contain a sources object with at most 1000 entries."
            )
        for source, expected in cast(dict[str, Any], sources).items():
            if not isinstance(expected, dict) or not expected:
                raise ValueError("Source report entries must be named objects.")
            actual = actual_sources.get(source)
            values: dict[str, Any] = actual.model_dump(mode="json") if actual is not None else {}
            if any(
                key not in values or values[key] != value
                for key, value in cast(dict[str, Any], expected).items()
            ):
                differences.append(f"source report {source}: binding differs")
    dbpath = data_root / DB_NAME
    if not dbpath.is_file():
        raise ValueError("Imported database is missing.")
    # A regular read-only connection observes a live WAL, unlike immutable=1.
    db = sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True)
    cards = actions = 0
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        for case in old.cases:
            row = db.execute(
                "SELECT state, title, goal, position, rules FROM cards WHERE handle=?",
                (derive("card", case.id),),
            ).fetchone()
            expected = "closed" if case.status == "closed" else "open"
            purpose = str(case.details.get("purpose") or "").strip()
            fields = (
                case.title.strip(),
                "" if purpose == case.title.strip() else purpose,
                case.position.strip(),
                card_rules(case, old.decisions),
            )
            if (
                row is None
                or row[0] != expected
                or (expected == "open" and tuple(row[1:]) != fields)
            ):
                differences.append(f"card {case.id}: absent or details differ")
            else:
                cards += 1
        open_cases = {case.id for case in old.cases if case.status != "closed"}
        for action in old.actions:
            if action.status not in OPEN_ACTION or action.case_id not in open_cases:
                continue
            row = db.execute(
                "SELECT card, state FROM actions WHERE handle=?", (derive("act", action.id),)
            ).fetchone()
            if row != (derive("card", action.case_id), "open"):
                differences.append(f"action {action.id}: absent, moved or state differs")
            else:
                actions += 1
        for approval in old.approvals:
            status = str(approval.details.get("status") or "approved").strip().lower()
            if status in DEAD_STATUSES:
                continue
            handle = derive("grant", approval.kind, approval.id)
            row = db.execute(
                "SELECT kind, state, scope_json, requirement, content_sha256 "
                "FROM grants WHERE handle=?",
                (handle,),
            ).fetchone()
            kind = "standing" if approval.kind == "standing" else "one_off"
            content = approval.details.get("scope_hash" if kind == "standing" else "message_hash")
            expected_hash = content if is_sha256(content) else sha256_text(f"import\n{approval.id}")
            if (
                row is None
                or row[0] != kind
                or row[2] is not None
                or row[3] != "human_confirmation"
                or row[4] != expected_hash
                or (
                    kind == "standing"
                    and row[1] not in {"suspended", "expired", "revoked", "lapsed"}
                )
            ):
                differences.append(f"grant {approval.id}: authority differs")
            if db.execute(
                "SELECT 1 FROM known_recipients WHERE grant=? LIMIT 1", (handle,)
            ).fetchone():
                differences.append(f"grant {approval.id}: unexpectedly confers recipient authority")
    finally:
        db.close()
    return Verification(cards, actions, tuple(differences))


def verify_migration(
    old_root: Path, data_root: Path, *, report_snapshot: Path | None = None
) -> MigrationStatus:
    """Compare the current v1 snapshot and v2 records without running either application."""
    _guard_paths(old_root, data_root)
    with _lock(data_root):
        state = _load(old_root, data_root)
        if state["stage"] not in {"imported", "verified", "switched"}:
            raise ValueError("Complete the import before verification.")
        result = _verify(old_root, data_root, report_snapshot)
        if state["stage"] != "switched":
            state["stage"] = "verified" if result.passed else "imported"
        state["verification"] = asdict(result)
        _atomic_json(_paths(data_root)[0], state)
        return MigrationStatus(str(state["stage"]), _paths(data_root)[1], result)


@dataclass(frozen=True)
class SwitchTarget:
    """Explicit schedule manifest consumed by the selected scheduler integration."""

    path: Path
    expected_sha256: str


@dataclass(frozen=True)
class WindowsTaskSwitch:
    """Exactly two operator-selected Task Scheduler entries and their expected actions."""

    old_name: str
    new_name: str
    old_command: str
    new_command: str
    old_arguments: str = ""
    new_arguments: str = ""


def _task_xml(name: str) -> ET.Element:
    result = subprocess.run(
        ["schtasks.exe", "/query", "/tn", name, "/xml"],
        check=True,
        capture_output=True,
    )
    raw = result.stdout
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    return ET.fromstring(text)


def _task_info(name: str, command: str, arguments: str) -> tuple[bool, bytes]:
    if not Path(command).is_absolute():
        raise ValueError("Expected task commands must be absolute paths.")
    root = _task_xml(name)
    namespace = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
    settings = root.find(namespace + "Settings")
    actions = root.find(namespace + "Actions")
    if settings is None or actions is None or len(actions) != 1:
        raise ValueError("Task must have settings and exactly one action.")
    action = actions[0]
    actual_command = action.findtext(namespace + "Command", default="")
    actual_arguments = action.findtext(namespace + "Arguments", default="")
    if (
        action.tag != namespace + "Exec"
        or (
            os.path.normcase(os.path.normpath(actual_command))
            != os.path.normcase(os.path.normpath(command))
        )
        or actual_arguments != arguments
    ):
        raise ValueError("Task action does not match the operator-selected command and arguments.")
    enabled = settings.find(namespace + "Enabled")
    if enabled is None or enabled.text not in {"true", "false"}:
        raise ValueError("Task enabled state is not explicit.")
    is_enabled = enabled.text == "true"
    enabled.text = "<state>"
    return is_enabled, ET.tostring(root, encoding="utf-8")


def _task_change(name: str, *, enabled: bool) -> None:
    subprocess.run(
        ["schtasks.exe", "/change", "/tn", name, "/enable" if enabled else "/disable"],
        check=True,
        capture_output=True,
    )


def _task_busy(name: str) -> bool:
    # Task Scheduler's running-task collection reflects active instances even after disabling.
    quoted = name.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; "
        "$service=New-Object -ComObject 'Schedule.Service'; $service.Connect(); "
        f"$name='{quoted}'; "
        "if (-not $name.StartsWith('\\')) { $name='\\' + $name }; "
        "$found=@($service.GetRunningTasks(0) | Where-Object { $_.Path -ieq $name }); "
        "if ($found.Count -gt 0) { 'busy' } else { 'idle' }"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True,
        capture_output=True,
    )
    state = result.stdout.decode("utf-8-sig").strip()
    if state not in {"busy", "idle"}:
        raise RuntimeError("Could not verify whether the old scheduled task is running.")
    return state == "busy"


def _switch_windows(target: WindowsTaskSwitch, expected: list[str] | None = None) -> None:
    if (
        not target.old_name
        or not target.new_name
        or target.old_name.lstrip("\\").casefold() == target.new_name.lstrip("\\").casefold()
    ):
        raise ValueError("Select two distinct scheduler tasks.")
    old_enabled, old_spec = _task_info(target.old_name, target.old_command, target.old_arguments)
    new_enabled, new_spec = _task_info(target.new_name, target.new_command, target.new_arguments)
    specs = [hashlib.sha256(old_spec).hexdigest(), hashlib.sha256(new_spec).hexdigest()]
    if expected is None:
        if not old_enabled or new_enabled:
            raise ValueError(
                "The old task must be enabled and the new task disabled before switching."
            )
    elif specs != expected or (old_enabled and new_enabled):
        raise ValueError(
            "Task settings or state changed since the switch began; inspect both tasks."
        )
    try:
        if old_enabled:
            _task_change(target.old_name, enabled=False)
        if _task_info(target.old_name, target.old_command, target.old_arguments) != (
            False,
            old_spec,
        ):
            raise RuntimeError("The old task did not disable cleanly.")
        if _task_busy(target.old_name):
            raise RuntimeError("The old task is still running; retry after it finishes.")
        if not new_enabled:
            _task_change(target.new_name, enabled=True)
        if _task_info(target.new_name, target.new_command, target.new_arguments) != (
            True,
            new_spec,
        ):
            raise RuntimeError("The new task did not enable cleanly.")
        if _task_info(target.old_name, target.old_command, target.old_arguments) != (
            False,
            old_spec,
        ):
            raise RuntimeError("The old task changed during the switch.")
    except Exception as error:
        try:
            _task_change(target.new_name, enabled=False)
            if _task_info(target.new_name, target.new_command, target.new_arguments) != (
                False,
                new_spec,
            ):
                raise RuntimeError("Could not verify new task disabled.")
            old_now, old_current = _task_info(
                target.old_name, target.old_command, target.old_arguments
            )
            if old_current != old_spec:
                raise RuntimeError("Old task settings changed.")
        except Exception as rollback:
            raise RuntimeError(
                "Task switch failed and rollback could not be verified; inspect both tasks."
            ) from rollback
        try:
            new_running = _task_busy(target.new_name)
        except Exception as rollback:
            raise RuntimeError(
                "Cannot verify whether the new task is running; inspect both tasks before retrying."
            ) from rollback
        if new_running:
            if old_now:
                raise RuntimeError(
                    "The new task is still running and the old task is enabled; inspect both tasks."
                ) from error
            raise RuntimeError(
                "Both tasks are disabled, but the new task is still running; wait for it to finish "
                "before restoring the old task."
            ) from error
        try:
            if not old_now:
                _task_change(target.old_name, enabled=True)
            if _task_info(target.old_name, target.old_command, target.old_arguments) != (
                True,
                old_spec,
            ):
                raise RuntimeError("Could not verify old task restored.")
        except Exception as rollback:
            raise RuntimeError(
                "Task switch failed and rollback could not be verified; inspect both tasks."
            ) from rollback
        raise RuntimeError(
            f"Task switch failed ({error}); old task was restored and new task disabled."
        ) from error


def switch_migration(
    old_root: Path,
    data_root: Path,
    *,
    switch: SwitchTarget | WindowsTaskSwitch,
    report_snapshot: Path | None = None,
) -> MigrationStatus:
    """Switch a selected manifest after rechecking parity; never alter its schedules."""
    _guard_paths(old_root, data_root)
    with _lock(data_root):
        state = _load(old_root, data_root)
        if state["stage"] not in {"verified", "switching", "switched"}:
            raise ValueError("A successful verification is required before switching.")
        if state["stage"] == "switched":
            result = _saved_verification(state)
            if result is None or not result.passed:
                raise ValueError("The saved switch has no successful verification.")
            if isinstance(switch, WindowsTaskSwitch):
                if state.get("switch_target") != asdict(switch):
                    raise ValueError("The saved switch used different tasks.")
                old_on, old_spec = _task_info(
                    switch.old_name, switch.old_command, switch.old_arguments
                )
                new_on, new_spec = _task_info(
                    switch.new_name, switch.new_command, switch.new_arguments
                )
                specs = [hashlib.sha256(old_spec).hexdigest(), hashlib.sha256(new_spec).hexdigest()]
                if (
                    old_on
                    or not new_on
                    or specs != state.get("switch_specs")
                    or _task_busy(switch.old_name)
                ):
                    raise ValueError(
                        "The selected task states changed since the switch; inspect both tasks."
                    )
            else:
                details = {
                    "kind": "sandbox",
                    "path": str(switch.path.resolve()),
                    "expected_sha256": switch.expected_sha256,
                }
                if state.get("switch_target") != details or (
                    hashlib.sha256(switch.path.read_bytes()).hexdigest()
                    != state.get("switch_new_sha256")
                ):
                    raise ValueError("The selected sandbox switch changed since completion.")
            return MigrationStatus("switched", _paths(data_root)[1], result)
        result = _verify(old_root, data_root, report_snapshot)
        if not result.passed:
            raise ValueError("The current installations differ; verify again before switching.")
        if isinstance(switch, WindowsTaskSwitch):
            details = asdict(switch)
            if state["stage"] == "verified":
                old_on, old_spec = _task_info(
                    switch.old_name, switch.old_command, switch.old_arguments
                )
                new_on, new_spec = _task_info(
                    switch.new_name, switch.new_command, switch.new_arguments
                )
                if (
                    not old_on
                    or new_on
                    or switch.old_name.lstrip("\\").casefold()
                    == switch.new_name.lstrip("\\").casefold()
                ):
                    raise ValueError("Select distinct tasks with old enabled and new disabled.")
                state["switch_target"] = details
                state["switch_specs"] = [
                    hashlib.sha256(old_spec).hexdigest(),
                    hashlib.sha256(new_spec).hexdigest(),
                ]
                state["stage"] = "switching"
                _atomic_json(_paths(data_root)[0], state)
            if state.get("switch_target") != details:
                raise ValueError("Resume with the same explicitly selected tasks.")
            _switch_windows(switch, state["switch_specs"])
        else:
            sandbox = (data_root / "import" / "sandbox").resolve()
            if sandbox not in switch.path.resolve().parents:
                raise ValueError("The sandbox schedule file must be under import/sandbox.")
            details = {
                "kind": "sandbox",
                "path": str(switch.path.resolve()),
                "expected_sha256": switch.expected_sha256,
            }
            raw = switch.path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if state["stage"] == "verified":
                if digest != switch.expected_sha256:
                    raise ValueError("Selected schedule file changed since review.")
                manifest: dict[str, Any] = json.loads(raw)
                if manifest.get("active") != "v1" or "schedules" not in manifest:
                    raise ValueError("Selected schedule file must have active=v1 and schedules.")
                manifest["active"] = "v2"
                new_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
                state["switch_target"] = details
                state["switch_new_sha256"] = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
                state["stage"] = "switching"
                _atomic_json(_paths(data_root)[0], state)
            if state.get("switch_target") != details:
                raise ValueError("Resume with the same selected sandbox file.")
            if digest == switch.expected_sha256:
                manifest = json.loads(raw)
                manifest["active"] = "v2"
                _atomic_json(switch.path, manifest)
            elif digest != state.get("switch_new_sha256"):
                raise ValueError("Selected sandbox file changed during the switch.")
        state["stage"] = "switched"
        state["verification"] = asdict(result)
        _atomic_json(_paths(data_root)[0], state)
        return MigrationStatus("switched", _paths(data_root)[1], result)
