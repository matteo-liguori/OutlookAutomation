"""Secret storage: the operating system credential store, else a restricted file.

Contract: docs/contracts/09-storage.md (S-17). No secret goes to the database, a
config file, a log or an artifact. The file fallback exists only for machines with
no usable credential store, and is readable by the current user alone.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

import keyring
import keyring.errors
from keyring.backends import fail

from chamberlain.schema.errors import ChamberlainError, ErrorCode


class SecretStore(Protocol):
    def get(self, service: str, name: str) -> str | None: ...

    def set(self, service: str, name: str, value: str) -> None: ...

    def delete(self, service: str, name: str) -> bool: ...


def restrict_to_user(path: Path) -> None:
    """Make a secret file or folder readable by the current user only."""
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        if user:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
                check=False,
            )
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


class KeyringStore:
    """The OS credential store through `keyring`."""

    def get(self, service: str, name: str) -> str | None:
        try:
            return keyring.get_password(service, name)
        except keyring.errors.KeyringError as error:
            raise _unavailable(error) from None

    def set(self, service: str, name: str, value: str) -> None:
        try:
            keyring.set_password(service, name, value)
        except keyring.errors.KeyringError as error:
            raise _unavailable(error) from None

    def delete(self, service: str, name: str) -> bool:
        try:
            keyring.delete_password(service, name)
        except keyring.errors.PasswordDeleteError:
            return False
        except keyring.errors.KeyringError as error:
            raise _unavailable(error) from None
        return True


class FileStore:
    """One JSON file per service under `folder`, restricted to the current user."""

    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def _path(self, service: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in service)
        return self._folder / f"{safe}.json"

    def _load(self, service: str) -> dict[str, str]:
        path = self._path(service)
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}

    def _save(self, service: str, data: dict[str, str]) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        restrict_to_user(self._folder)
        path = self._path(service)
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(data), encoding="utf-8")
        restrict_to_user(pending)
        pending.replace(path)

    def get(self, service: str, name: str) -> str | None:
        return self._load(service).get(name)

    def set(self, service: str, name: str, value: str) -> None:
        data = self._load(service)
        data[name] = value
        self._save(service, data)

    def delete(self, service: str, name: str) -> bool:
        data = self._load(service)
        if name not in data:
            return False
        del data[name]
        self._save(service, data)
        return True


def keyring_usable() -> bool:
    """True when `keyring` has a real backend (not the failing placeholder)."""
    backend = keyring.get_keyring()
    return not isinstance(backend, fail.Keyring) and getattr(backend, "priority", 1) > 0


def default_store(data_root: Path) -> SecretStore:
    """The credential store when there is one, else the restricted file under `secrets/`."""
    if keyring_usable():
        return KeyringStore()
    print("No operating-system credential store found; using a restricted file.", file=sys.stderr)
    return FileStore(data_root / "secrets")


def _unavailable(error: Exception) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.SOURCE_UNAVAILABLE,
        "The operating system credential store did not answer.",
        detail=type(error).__name__,
    )
