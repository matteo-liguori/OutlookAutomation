"""CLI for an explicitly selected v1 installation and its resumable migration."""

import argparse
import json
import os
import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from chamberlain.importer.migrate import (
    SwitchTarget,
    WindowsTaskSwitch,
    detect_migration,
    migration_status,
    prepare_migration,
    switch_migration,
    verify_migration,
)
from chamberlain.schema.errors import ChamberlainError
from chamberlain.surface.runner import DATA_ENV


def migration_parser(parser: argparse.ArgumentParser) -> None:
    steps = parser.add_subparsers(dest="action", required=True)
    for name in ("detect", "status", "prepare", "verify", "switch"):
        step = steps.add_parser(name)
        step.add_argument("old_root", type=Path, help="v1 data folder")
        if name in {"verify", "switch"}:
            step.add_argument("--report-snapshot", type=Path, help="optional offline comparison")
        if name == "switch":
            targets = step.add_mutually_exclusive_group(required=True)
            targets.add_argument("--old-task", help="full name of the installed v1 scheduled task")
            targets.add_argument(
                "--sandbox-manifest", type=Path, help="isolated rehearsal schedule"
            )
            step.add_argument("--expected-sha256")
            step.add_argument("--new-task")
            step.add_argument("--old-command")
            step.add_argument("--new-command")
            step.add_argument("--old-arguments", default="")
            step.add_argument("--new-arguments", default="")


def _switch_target(args: argparse.Namespace) -> SwitchTarget | WindowsTaskSwitch:
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


def migration_command(args: argparse.Namespace, out: TextIO) -> int:
    try:
        if args.action == "detect":
            print(json.dumps(detect_migration(args.old_root), indent=2), file=out)
            return 0
        data = args.data or os.environ.get(DATA_ENV)
        if not data:
            raise ValueError(f"Set {DATA_ENV} or pass --data before the command.")
        root = Path(data)
        if args.action == "prepare":
            result = prepare_migration(args.old_root, root)
        elif args.action == "verify":
            result = verify_migration(args.old_root, root, report_snapshot=args.report_snapshot)
        elif args.action == "switch":
            result = switch_migration(
                args.old_root,
                root,
                switch=_switch_target(args),
                report_snapshot=args.report_snapshot,
            )
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
