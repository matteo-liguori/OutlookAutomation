"""Reading, validating and writing `config/connections.yaml`.

Contract: docs/contracts/09-storage.md (S-16). No module reads this at import (D-12).
A missing file is an empty profile (no sources); a malformed one is `config.invalid`
with the offending field named, never a traceback.
"""

import json
import os
import shutil
import tempfile
from codecs import BOM_UTF8
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, ScalarNode

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
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise _invalid("The connection profile cannot be read.", detail=str(error)[:300]) from None
    return _parse_profile(text)


def _parse_profile(text: str) -> Connections:
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise _invalid("The connection profile cannot be read.", detail=str(error)[:300]) from None
    if raw is not None and not isinstance(raw, dict):
        raise _invalid("The connection profile must be a mapping.")
    return parse_connections(raw)  # pyright: ignore[reportUnknownArgumentType]


def _member(mapping: MappingNode, name: str) -> MappingNode | None:
    for key, value in reversed(mapping.value):
        if isinstance(key, ScalarNode) and key.value == name:
            if not isinstance(value, MappingNode):
                raise _invalid(f"The {name} section must be a mapping.", field=name)
            return value
    return None


def _insert_calendar(text: str, source: str) -> str:
    """Use YAML node positions to insert a mapping entry and preserve surrounding text."""
    top = yaml.compose(text, Loader=yaml.SafeLoader)  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(top, MappingNode):
        raise _invalid("The connection profile must be a mapping.")
    keys = [json.dumps(source)]
    live = _member(top, "live_test")
    if live is None:
        keys[:0] = ["live_test", "calendars"]
        target = top
    else:
        calendars = _member(live, "calendars")
        if calendars is None:
            keys.insert(0, "calendars")
        target = live if calendars is None else calendars

    if target.flow_style:
        start = text.index("{", target.start_mark.index, target.end_mark.index) + 1
        entry = "{}"
        for key in reversed(keys):
            entry = f"{{{key}: {entry}}}"
        insert = entry[1:-1] + (", " if target.value else "")
    else:
        mark = target.value[0][0].start_mark
        start = mark.index - mark.column
        if text[start : mark.index].strip():
            raise _invalid("The calendar could not be added to this YAML layout.")
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = [" " * (mark.column + depth * 2) + key + ":" for depth, key in enumerate(keys)]
        lines[-1] += " {}"
        insert = newline.join(lines) + newline
    return text[:start] + insert + text[start:]


def add_test_calendar(data_root: Path, source: str) -> Path | None:
    """Enroll an existing calendar. Return the exact-byte backup path after a change."""
    refuse_inside_checkout(data_root)
    path = profile_path(data_root)
    try:
        before = path.read_bytes()
        text = before.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise _invalid("The connection profile cannot be read.", detail=str(error)[:300]) from None
    current = _parse_profile(text)
    if not source.startswith("calendar:") or source not in current.sources:
        raise _invalid("Choose a configured calendar source ID.", field="source")
    if source in current.live_test.calendars:
        return None

    changed = _insert_calendar(text, source)
    after = _parse_profile(changed)
    expected = to_document(current)
    expected["live_test"]["calendars"][source] = {}
    if after != parse_connections(expected):
        raise _invalid("Adding the test calendar would also change other settings.")

    bom = BOM_UTF8 if before.startswith(BOM_UTF8) else b""
    try:
        with tempfile.TemporaryDirectory(dir=path.parent) as staging:
            pending = Path(staging) / path.name
            pending.write_bytes(bom + changed.encode("utf-8"))
            shutil.copymode(path, pending)
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f"{path.name}.bak-", delete=False
            ) as backup:
                backup.write(before)
            if path.read_bytes() != before:
                raise _invalid("The connection profile changed while it was being edited.")
            os.replace(pending, path)
    except OSError as error:
        raise _invalid(
            "The connection profile could not be updated.", detail=str(error)[:300]
        ) from None
    return Path(backup.name)


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
