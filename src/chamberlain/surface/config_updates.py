"""CLI commands for a local store migration and release notes."""

import argparse
import json
from pathlib import Path
from typing import TextIO

from chamberlain.connections.migrate_config import migrate_config
from chamberlain.runs.updates import release_notes
from chamberlain.schema.envelope import error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode


def _error(out: TextIO, error: ChamberlainError) -> int:
    out.write(error_reply(error).model_dump_json(exclude_none=True) + "\n")
    return 1


def config_migrate(data_root: Path, *, apply: bool, out: TextIO) -> int:
    """Preview the numbered upgrade, or apply it with a backup."""
    try:
        result = migrate_config(data_root, apply=apply)
    except ChamberlainError as error:
        return _error(out, error)
    out.write(
        json.dumps(
            {
                "mode": "apply" if apply else "preview",
                "profile": str(result.profile) if result.profile else None,
                "store_version": result.store_version,
                "pending": result.pending,
                "applied": result.applied,
                "backup": str(result.backup) if result.backup else None,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


def updates(args: argparse.Namespace, out: TextIO) -> int:
    """Print the notes from an exact local release tag."""
    try:
        notes = release_notes(args.tag)
        if notes is None:
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The local tag has no release notes or is absent.",
                field="tag",
                fix="Use an annotated release tag present in this checkout.",
            )
    except ChamberlainError as error:
        return _error(out, error)
    out.write(notes + "\n")
    return 0
