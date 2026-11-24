"""Moving the old Google and iCloud secrets into the credential store.

The old files are copied and left where they are (the old data root is the fallback). A
secret that is already in the store is never overwritten, even by a different value.
"""

import base64
import binascii
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from chamberlain.connectors.caldav.credentials import SERVICE as CALDAV_SERVICE
from chamberlain.connectors.caldav.credentials import credential_name
from chamberlain.connectors.calendar_support.secrets import SecretStore
from chamberlain.connectors.google_calendar import auth
from chamberlain.importer import dpapi
from chamberlain.importer.report import ImportReport
from chamberlain.schema.errors import ChamberlainError

GOOGLE_CLIENT = Path("google") / "client.json"
GOOGLE_TOKENS = Path("google") / "tokens"
ICLOUD_FILE = "icloud-app-password.json"
"""The old harness's plain file (the Mac setup)."""
ICLOUD_WINDOWS_FILE = "icloud-caldav.json"
"""The old harness's DPAPI-encrypted file (the Windows setup)."""
_WINDOWS_ENTROPY = b"OutlookAutomation-iCloud-CalDAV-v2"


def legacy_windows_dirs() -> list[Path]:
    """Where the oldest Windows setup kept its file, outside the data root."""
    local = os.environ.get("LOCALAPPDATA")
    return [Path(local) / "Codex" / "OutlookAutomation"] if local else []


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None  # pyright: ignore[reportUnknownVariableType]


def _windows_credential(path: Path, unprotect: dpapi.Unprotect) -> tuple[str, str] | None:
    data = _read(path)
    if data is None:
        return None
    username = str(data.get("appleAccount", "")).strip()
    blob = str(data.get("encryptedPassword", "")).strip()
    version = data.get("schemaVersion")
    if not username or not blob:
        return None
    try:
        if version == 2:
            password = unprotect(base64.b64decode(blob, validate=True), _WINDOWS_ENTROPY)
            text = password.decode("utf-8")
        elif version == 1:
            text = unprotect(bytes.fromhex(blob), None).decode("utf-16-le")
        else:
            return None
    except (OSError, ValueError, binascii.Error):
        return None
    return (username, text.strip()) if text.strip() else None


def icloud_credential(
    root: Path,
    *,
    unprotect: dpapi.Unprotect | None = None,
    extra_dirs: Sequence[Path] | None = None,
) -> tuple[str, str] | None:
    """The saved iCloud Apple ID and app-specific password, or None.

    Looks for the old Mac file, then the old Windows file in the old data root, then the oldest
    Windows location. The first readable one wins.
    """
    data = _read(root / ICLOUD_FILE)
    if data is not None:
        username, password = (
            str(data.get("username", "")).strip(),
            str(data.get("password", "")).strip(),
        )
        if username and password:
            return username, password
    dirs = [root, *(legacy_windows_dirs() if extra_dirs is None else extra_dirs)]
    for folder in dirs:
        found = _windows_credential(folder / ICLOUD_WINDOWS_FILE, unprotect or dpapi.unprotect)
        if found is not None:
            return found
    return None


def move_secrets(
    root: Path,
    store: SecretStore,
    report: ImportReport,
    icloud: tuple[str, str] | None = None,
) -> bool:
    """Copy every old secret that the store does not hold yet. True when it wrote any.

    `icloud` is the credential the caller already found (`icloud_credential`)."""
    wrote = False

    client = _read(root / GOOGLE_CLIENT)
    installed: dict[str, Any] | None = cast(
        dict[str, Any] | None, client.get("installed") if client else None
    )
    if isinstance(installed, dict):
        try:
            auth.load_client(store)
        except ChamberlainError:
            try:
                auth.save_client(
                    store, str(installed["client_id"]), str(installed["client_secret"])
                )
            except (ChamberlainError, KeyError):
                report.warnings.append("The old Google client file is not usable; link again.")
            else:
                report.secrets["google client"] = "copied"
                wrote = True
        else:
            report.secrets["google client"] = "already present"

    tokens = root / GOOGLE_TOKENS
    for path in sorted(tokens.glob("*.json")) if tokens.is_dir() else []:
        email = path.stem.strip().lower()
        label = f"google token {email}"
        if email in auth.accounts(store):
            report.secrets[label] = "already present"
            continue
        data = _read(path)
        refresh = str(data.get("refresh_token", "")) if data else ""
        if not refresh:
            report.warnings.append(f"The old Google token for {email} has no refresh token.")
            continue
        auth.save_refresh_token(store, email, refresh, str(data.get("scope") or "") if data else "")
        report.secrets[label] = "copied"
        wrote = True

    saved = icloud
    if saved is not None:
        username, password = saved
        name = credential_name(username)
        existing = store.get(CALDAV_SERVICE, name)
        if existing is None:
            store.set(CALDAV_SERVICE, name, password)
            report.secrets["icloud app password"] = "copied"
            wrote = True
        elif existing == password:
            report.secrets["icloud app password"] = "already present"
        else:
            report.secrets["icloud app password"] = "differs; kept the existing one"
    return wrote
