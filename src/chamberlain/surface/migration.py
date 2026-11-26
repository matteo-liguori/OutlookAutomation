"""CLI for an explicitly selected v1 installation and its resumable migration."""

import argparse
import json
import os
import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from chamberlain.connectors.caldav.setup import verify as icloud_verify
from chamberlain.connectors.thunderbird.update import SystemDesktop, thunderbird_home
from chamberlain.importer.archive import (
    DEFAULT_KEEP_DAYS,
    archive_v1,
    path_in_use,
    system_scheduler,
)
from chamberlain.importer.automations import (
    detect_app_automations,
    guess_checkout,
    pause_app_automations,
)
from chamberlain.importer.migrate import (
    SwitchTarget,
    WindowsTaskSwitch,
    backup_path,
    detect_migration,
    migration_status,
    old_standing_grants,
    prepare_migration,
    switch_migration,
    verify_migration,
)
from chamberlain.importer.retire import retire_v1_addon, system_native_host, system_processes
from chamberlain.schema.errors import ChamberlainError
from chamberlain.surface.runner import DATA_ENV


def app_homes() -> tuple[Path, Path]:
    """The agent apps' folders (Codex, Claude). Tests replace this."""
    return Path.home() / ".codex", Path.home() / ".claude"


def _parser_options(step: argparse.ArgumentParser, name: str) -> None:
    step.add_argument("old_root", type=Path, help="v1 data folder")
    if name in {"detect", "prepare", "switch", "archive"}:
        step.add_argument(
            "--old-checkout", type=Path, help="v1 source folder (default: found next to OLD_ROOT)"
        )
    if name in {"verify", "switch"}:
        step.add_argument("--report-snapshot", type=Path, help="optional offline comparison")
    if name in {"switch", "archive"}:
        step.add_argument("--apply", action="store_true", help="act; without it only show the plan")
    if name == "switch":
        targets = step.add_mutually_exclusive_group(required=False)
        targets.add_argument("--old-task", help="full name of the installed v1 scheduled task")
        targets.add_argument("--sandbox-manifest", type=Path, help="isolated rehearsal schedule")
        step.add_argument("--expected-sha256")
        step.add_argument("--new-task")
        step.add_argument("--old-command")
        step.add_argument("--new-command")
        step.add_argument("--old-arguments", default="")
        step.add_argument("--new-arguments", default="")
        step.add_argument(
            "--pause-app-automations",
            action="store_true",
            help="pause the agent-app automations that run v1 (Codex: backup, then PAUSED)",
        )
        step.add_argument(
            "--keep-v1-addon",
            action="store_true",
            help="do not archive v1's Thunderbird add-on and native host",
        )
    if name == "archive":
        step.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
        step.add_argument("--also", type=Path, nargs="*", default=[], help="more folders to move")
        step.add_argument(
            "--in-use", type=Path, nargs="*", default=[], help="folders to move later, not now"
        )
        step.add_argument(
            "--confirm-switched",
            action="store_true",
            help="archive although the migration did not record a switch (switched by hand)",
        )


def migration_parser(parser: argparse.ArgumentParser) -> None:
    steps = parser.add_subparsers(dest="action", required=True)
    for name in ("detect", "status", "prepare", "verify", "switch", "grants", "archive"):
        _parser_options(steps.add_parser(name), name)


def _switch_target(args: argparse.Namespace) -> SwitchTarget | WindowsTaskSwitch | None:
    if args.sandbox_manifest is None and args.old_task is None:
        if any((args.expected_sha256, args.new_task, args.old_command, args.new_command)):
            raise ValueError("Name --old-task or --sandbox-manifest with those options.")
        return None
    if args.sandbox_manifest is not None:
        if not args.expected_sha256 or any(
            (
                args.new_task,
                args.old_command,
                args.new_command,
                args.old_arguments,
                args.new_arguments,
            )
        ):
            raise ValueError("Select a sandbox manifest and its expected SHA256 together.")
        return SwitchTarget(args.sandbox_manifest, args.expected_sha256)
    if args.expected_sha256 or not all((args.new_task, args.old_command, args.new_command)):
        raise ValueError("Select both scheduled tasks and specify both expected commands.")
    return WindowsTaskSwitch(
        args.old_task,
        args.new_task,
        args.old_command,
        args.new_command,
        args.old_arguments,
        args.new_arguments,
    )


def _checkout(args: argparse.Namespace) -> Path | None:
    return args.old_checkout or guess_checkout(args.old_root)


def _data_root(args: argparse.Namespace) -> Path:
    data = args.data or os.environ.get(DATA_ENV)
    if not data:
        raise ValueError(f"Set {DATA_ENV} or pass --data before the command.")
    return Path(data)


def _switch(args: argparse.Namespace, root: Path) -> dict[str, object]:
    target = _switch_target(args)
    result: dict[str, object]
    if target is not None:
        status = switch_migration(
            args.old_root, root, switch=target, report_snapshot=args.report_snapshot
        )
        result = asdict(status)
    else:
        status = migration_status(args.old_root, root)
        if status.stage not in {"verified", "switching", "switched"}:
            raise ValueError("Verify the migration (migrate verify) before switching.")
        result = {"stage": status.stage}
    checkout = _checkout(args)
    codex, claude = app_homes()
    if args.pause_app_automations:
        result["app_automations"] = pause_app_automations(
            checkout,
            codex_home=codex,
            claude_home=claude,
            backup_dir=root / "import" / "app-automation-backups",
            apply=args.apply,
        )
    if not args.keep_v1_addon:
        retired = retire_v1_addon(
            root / "import" / "v1-retired",
            apply=args.apply,
            desktop=SystemDesktop(),
            host=system_native_host(),
            home=thunderbird_home(),
        )
        result["v1_addon"] = {"applied": retired.applied, "steps": retired.steps}
    if not args.apply:
        result["note"] = "Dry run for the app automations and the v1 add-on: add --apply to act."
    return result


def _archive(args: argparse.Namespace) -> dict[str, object]:
    data = args.data or os.environ.get(DATA_ENV)
    root = Path(data) if data else None
    backup: Path | None = None
    copies: list[Path] = []
    if root is not None:
        backup = backup_path(root)
        copies = [root / "import" / "v1-retired", root / "import" / "app-automation-backups"]
        stage = migration_status(args.old_root, root).stage
        if args.apply and stage != "switched" and not args.confirm_switched:
            raise ValueError(
                f"The migration is at stage {stage!r}, not switched. Switch first, or pass "
                "--confirm-switched if you switched by hand."
            )
    elif args.apply and not args.confirm_switched:
        raise ValueError("Pass --data so the switch can be checked, or --confirm-switched.")
    forced = [p.resolve() for p in args.in_use]

    def in_use(path: Path) -> bool:
        return path.resolve() in forced or path_in_use(path)

    result = archive_v1(
        old_root=args.old_root,
        old_checkout=_checkout(args),
        import_backup=backup if backup is not None and backup.exists() else None,
        also=args.also,
        extra_copies=copies,
        keep_days=args.keep_days,
        apply=args.apply,
        scheduler=system_scheduler(),
        in_use=in_use,
    )
    return result.document()


def migration_command(args: argparse.Namespace, out: TextIO) -> int:
    try:
        if args.action == "detect":
            document: dict[str, object] = dict(detect_migration(args.old_root))
            codex, claude = app_homes()
            document["app_automations"] = detect_app_automations(
                _checkout(args), codex_home=codex, claude_home=claude
            )
            print(json.dumps(document, indent=2), file=out)
            return 0
        if args.action == "grants":
            print(json.dumps(old_standing_grants(args.old_root), indent=2), file=out)
            return 0
        if args.action == "archive":
            print(json.dumps(_archive(args), indent=2), file=out)
            return 0
        root = _data_root(args)
        if args.action == "switch":
            print(json.dumps(_switch(args, root), default=str, indent=2), file=out)
            return 0
        if args.action == "prepare":
            result = prepare_migration(
                args.old_root,
                root,
                caldav_check=icloud_verify,
                processes=system_processes,
                old_checkout=args.old_checkout,
            )
        elif args.action == "verify":
            result = verify_migration(args.old_root, root, report_snapshot=args.report_snapshot)
        else:
            result = migration_status(args.old_root, root)
        print(json.dumps(asdict(result), default=str, indent=2), file=out)
        return int(result.verification is not None and not result.verification.passed)
    except (OSError, subprocess.SubprocessError) as error:
        print(
            json.dumps({"error": f"File or scheduled task unavailable ({type(error).__name__})."}),
            file=out,
        )
        return 1
    except (ChamberlainError, ValueError, sqlite3.Error, RuntimeError) as error:
        print(json.dumps({"error": str(error)}), file=out)
        return 1
