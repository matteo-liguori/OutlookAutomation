"""Guided CalDAV setup, written for iCloud and usable for any standard server.

    python -m chamberlain.connectors.caldav.setup credential [--apple-id you@example.com]
    python -m chamberlain.connectors.caldav.setup check [--apple-id ...]
    python -m chamberlain.connectors.caldav.setup register [--apple-id ...] [--calendar NAME]
    python -m chamberlain.connectors.caldav.setup forget [--apple-id ...]

Apple has no sign-in flow for outside tools: create an app-specific password for your
Apple account and this command checks it against iCloud before it saves anything. The
password goes to the operating system credential store (S-17); the connection profile
holds only the calendar address, the user name and the credential's name.
"""

import argparse
import base64
import getpass
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

import yaml

from chamberlain.connectors.caldav import dav
from chamberlain.connectors.caldav.credentials import SERVICE, credential_name
from chamberlain.connectors.calendar_support.http import (
    HttpReply,
    HttpTransport,
    TransportFailure,
    UrllibTransport,
    require_secure,
)
from chamberlain.connectors.calendar_support.secrets import SecretStore, default_store
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

ICLOUD_URL = "https://caldav.icloud.com/"
ICLOUD_SOURCE = "calendar:icloud"
APP_PASSWORD = re.compile(r"^[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3}$")
PROFILE_VERSION = 1

Say = Callable[[str], None]
Check = Callable[[str, str], list["Discovered"]]


@dataclass(frozen=True)
class Discovered:
    name: str
    url: str


def profile_path(data_root: Path) -> Path:
    """The private connection profile (S-16)."""
    return data_root / "config" / "connections.yaml"


def _propfind(
    transport: HttpTransport, url: str, body: bytes, depth: str, auth: str
) -> list[dav.DavResponse]:
    try:
        reply: HttpReply = transport.send(
            "PROPFIND",
            require_secure(url),
            headers={"Authorization": auth, "Depth": depth, "Content-Type": "application/xml"},
            body=body,
            timeout=30,
        )
    except TransportFailure as error:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE,
            "Could not reach the calendar server.",
            detail=str(error),
        ) from None
    if reply.status in (401, 403):
        raise ChamberlainError(
            ErrorCode.SOURCE_SIGN_IN_NEEDED,
            "The server refused the sign-in: check the Apple ID and use an app-specific "
            "password, not your normal Apple password.",
        )
    if reply.status != 207:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE,
            "The calendar server gave an unexpected answer.",
            detail=f"http {reply.status}",
        )
    try:
        return dav.parse_multistatus(reply.body)
    except ValueError:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE, "The calendar server answer was unreadable."
        ) from None


def discover(
    base_url: str, username: str, password: str, transport: HttpTransport | None = None
) -> list[Discovered]:
    """Sign in and list the account's event calendars: principal, then home set, then calendars."""
    http = transport or UrllibTransport()
    auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    principal = next(
        (
            r.principal
            for r in _propfind(http, base_url, dav.PROPFIND_PRINCIPAL, "0", auth)
            if r.principal
        ),
        None,
    )
    if principal is None:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE, "The server named no calendar account."
        )
    principal_url = urljoin(base_url, principal)
    home = next(
        (
            r.home_set
            for r in _propfind(http, principal_url, dav.PROPFIND_HOME, "0", auth)
            if r.home_set
        ),
        None,
    )
    if home is None:
        raise ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, "The server named no calendar home.")
    home_url = urljoin(principal_url, home)
    return sorted(
        (
            Discovered(r.display_name or "", urljoin(home_url, r.href))
            for r in _propfind(http, home_url, dav.PROPFIND_CALENDARS, "1", auth)
            if r.is_calendar and r.supports_events
        ),
        key=lambda d: d.name.casefold(),
    )


def verify(username: str, password: str) -> list[Discovered]:
    return discover(ICLOUD_URL, username, password)


def save_credential(
    store: SecretStore, username: str, password: str, check: Check = verify
) -> list[Discovered]:
    """Save the password only after the server has accepted it."""
    username, password = username.strip(), password.strip()
    if "@" not in username:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "Use your Apple ID email address.", field="apple_id"
        )
    if not APP_PASSWORD.fullmatch(password):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "That is not an app-specific password; it looks like xxxx-xxxx-xxxx-xxxx.",
            field="password",
        )
    calendars = check(username, password)
    store.set(SERVICE, credential_name(username), password)
    return calendars


def forget(store: SecretStore, username: str) -> bool:
    return store.delete(SERVICE, credential_name(username))


def saved_password(store: SecretStore, username: str) -> str:
    password = store.get(SERVICE, credential_name(username))
    if password is None:
        raise ChamberlainError(
            ErrorCode.SOURCE_NOT_CONFIGURED,
            "No saved iCloud credential.",
            fix='Run "python -m chamberlain.connectors.caldav.setup credential".',
        )
    return password


def register(
    path: Path,
    source: str,
    username: str,
    calendar: Discovered,
    clock: Clock | None = None,
) -> bool:
    """Add the calendar to the private connection profile, keeping a backup. Idempotent."""
    entry: dict[str, Any] = {
        "kind": "caldav",
        "url": calendar.url,
        "username": username,
        "credential": credential_name(username),
    }
    profile: dict[str, Any] = {"version": PROFILE_VERSION, "sources": {}}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            profile = cast(dict[str, Any], loaded)
    sources = profile.setdefault("sources", {})
    if sources.get(source) == entry:
        return False
    sources[source] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = (clock or SystemClock()).now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
    pending = path.with_suffix(".tmp")
    pending.write_text(_dump(profile), encoding="utf-8")
    pending.replace(path)
    return True


def _dump(profile: dict[str, Any]) -> str:
    return yaml.safe_dump(profile, sort_keys=True)  # pyright: ignore[reportUnknownMemberType]


def _data_root(data: str | None) -> Path:
    root = data or os.environ.get("CHAMBERLAIN_DATA")
    if not root:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "Give the data root with --data or CHAMBERLAIN_DATA.",
            field="data",
        )
    return Path(root)


def main(argv: list[str] | None = None, *, say: Say = print) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chamberlain.connectors.caldav.setup",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data", help="the data root (default: CHAMBERLAIN_DATA)")
    sub = parser.add_subparsers(dest="step", required=True)
    for name, text in (
        ("credential", "check an app-specific password against iCloud and save it"),
        ("check", "sign in with the saved credential and list your calendars"),
        ("register", "add an iCloud calendar to your private connection profile"),
        ("forget", "delete the saved credential (revoke the password at account.apple.com too)"),
    ):
        step = sub.add_parser(name, help=text)
        step.add_argument("--apple-id")
        if name == "register":
            step.add_argument("--calendar", help="the calendar's name (default: the only one)")
            step.add_argument("--source", default=ICLOUD_SOURCE)
    args = parser.parse_args(argv)
    try:
        root = _data_root(args.data)
        store = default_store(root)
        apple_id = args.apple_id or input("Apple ID email: ")
        if args.step == "credential":
            say("Create an app-specific password at https://account.apple.com/account/manage")
            say("(Sign-In and Security), label it Chamberlain, then paste it below. It is hidden.")
            found = save_credential(store, apple_id, getpass.getpass("App-specific password: "))
            say("Saved the password to the credential store.")
            say("Your iCloud calendars: " + ", ".join(c.name for c in found if c.name))
            say("Next: register")
        elif args.step == "check":
            found = verify(apple_id, saved_password(store, apple_id))
            say(f"{apple_id}: ok")
            say("Calendars: " + ", ".join(c.name for c in found if c.name))
        elif args.step == "register":
            found = verify(apple_id, saved_password(store, apple_id))
            chosen = [c for c in found if args.calendar in (None, c.name)]
            if len(chosen) != 1:
                names = ", ".join(c.name for c in found)
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, f"Name one calendar with --calendar: {names}."
                )
            changed = register(profile_path(root), args.source, apple_id, chosen[0])
            say("Added to the profile." if changed else "Already in the profile.")
        else:
            say(
                "Removed the saved credential."
                if forget(store, apple_id)
                else "No saved credential."
            )
    except (ChamberlainError, OSError) as error:
        print(f"Setup stopped: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
