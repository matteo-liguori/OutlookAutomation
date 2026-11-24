"""Moving the old Google and iCloud secrets into the credential store.

The old files are copied and left where they are (the old data root is the fallback). A
secret that is already in the store is never overwritten, even by a different value.
"""

import json
from pathlib import Path
from typing import Any, cast

from chamberlain.connectors.caldav.credentials import SERVICE as CALDAV_SERVICE
from chamberlain.connectors.caldav.credentials import credential_name
from chamberlain.connectors.calendar_support.secrets import SecretStore
from chamberlain.connectors.google_calendar import auth
from chamberlain.importer.report import ImportReport
from chamberlain.schema.errors import ChamberlainError

GOOGLE_CLIENT = Path("google") / "client.json"
GOOGLE_TOKENS = Path("google") / "tokens"
ICLOUD_FILE = "icloud-app-password.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None  # pyright: ignore[reportUnknownVariableType]


def icloud_credential(root: Path) -> tuple[str, str] | None:
    """The saved iCloud Apple ID and app-specific password, or None."""
    data = _read(root / ICLOUD_FILE)
    if data is None:
        return None
    username, password = (
        str(data.get("username", "")).strip(),
        str(data.get("password", "")).strip(),
    )
    return (username, password) if username and password else None


def move_secrets(root: Path, store: SecretStore, report: ImportReport) -> bool:
    """Copy every old secret that the store does not hold yet. True when it wrote any."""
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

    saved = icloud_credential(root)
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
