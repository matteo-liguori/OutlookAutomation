"""Reading, validating and writing `config/connections.yaml`.

Contract: docs/contracts/09-storage.md (S-16). No module reads this at import (D-12).
A missing file is an empty profile (no sources); a malformed one is `config.invalid`
with the offending field named, never a traceback.
"""

import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import ValidationError

from chamberlain.connections.model import Connections
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.schema.errors import ChamberlainError, ErrorCode

CONFIG_DIR = "config"
CONFIG_FILE = "connections.yaml"


def profile_path(data_root: Path) -> Path:
    return data_root / CONFIG_DIR / CONFIG_FILE


def _invalid(message: str, field: str | None = None, detail: str = "") -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.CONFIG_INVALID,
        message,
        field=field,
        fix=f"Correct {CONFIG_DIR}/{CONFIG_FILE} and run `chamberlain config validate`.",
        detail=detail,
    )


def parse_connections(raw: object) -> Connections:
    """Validate an already-parsed document."""
    try:
        connections = Connections.model_validate(raw if raw is not None else {})
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or None
        # Pydantic echoes the offending input; keep it out of the message (it may be a secret).
        raise _invalid(
            f"The connection profile is not valid: {first['msg']}", field=field
        ) from None
    try:
        ZoneInfo(connections.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise _invalid("The time zone is not known.", field="timezone") from None
    return connections


def load_connections(data_root: Path) -> Connections:
    """The profile under a data root. Missing file: no sources."""
    refuse_inside_checkout(data_root)
    path = profile_path(data_root)
    if not path.is_file():
        return Connections()
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as error:
        raise _invalid("The connection profile cannot be read.", detail=str(error)[:300]) from None
    if raw is not None and not isinstance(raw, dict):
        raise _invalid("The connection profile must be a mapping.")
    return parse_connections(raw)  # pyright: ignore[reportUnknownArgumentType]


def to_document(connections: Connections) -> dict[str, Any]:
    """The plain-data form that is written to disk and shown by `config show`."""
    return connections.model_dump(mode="json", exclude_none=True)


def dump_connections(connections: Connections) -> str:
    return yaml.safe_dump(  # pyright: ignore[reportUnknownMemberType]
        to_document(connections), sort_keys=True, allow_unicode=True
    )


def save_connections(data_root: Path, connections: Connections) -> bool:
    """Write the profile atomically. False when the file already holds exactly this text."""
    path = profile_path(data_root)
    text = dump_connections(connections)
    if path.is_file() and path.read_text(encoding="utf-8-sig") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(text, encoding="utf-8")
    os.replace(pending, path)
    return True


def render_redacted(connections: Connections) -> dict[str, Any]:
    """What `config show` prints. The profile holds no secret, so this is the document itself.

    Credential names stay (they are references into the credential store, not secrets).
    """
    return to_document(connections)
