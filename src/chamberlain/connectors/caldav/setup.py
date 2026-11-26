"""Guided CalDAV setup, written for iCloud and usable for any standard server.

    python -m chamberlain.connectors.caldav.setup credential [--apple-id you@example.com]
    python -m chamberlain.connectors.caldav.setup check [--apple-id ...]
    python -m chamberlain.connectors.caldav.setup register [--apple-id ...] [--calendar NAME]
    python -m chamberlain.connectors.caldav.setup forget [--apple-id ...]
    python -m chamberlain.connectors.caldav.setup import-old --old-data OLD_DATA_ROOT

Apple has no sign-in flow for outside tools: create an app-specific password for your
Apple account and this command checks it against iCloud before it saves anything. The
password goes to the operating system credential store (S-17); the connection profile
holds only the calendar address, the user name and the credential's name.
"""

import argparse
import base64
import getpass
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

import yaml

from chamberlain.connectors.caldav import dav
from chamberlain.connectors.caldav.credentials import SERVICE, credential_name
from chamberlain.connectors.calendar_support.http import (
    HttpTransport,
    UrllibTransport,
    calendar_unavailable,
    check_read_status,
    read_http,
    require_secure,
)
from chamberlain.connectors.calendar_support.secrets import SecretStore, default_store
from chamberlain.dataroot import data_root_arg
from chamberlain.importer.credentials import icloud_credential
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
    reply = read_http(
        lambda: transport.send(
            "PROPFIND",
            require_secure(url),
            headers={"Authorization": auth, "Depth": depth, "Content-Type": "application/xml"},
            body=body,
            timeout=30,
        ),
        partial(calendar_unavailable, message="Could not reach the calendar server."),
    )
    check_read_status(
        reply,
        207,
        partial(calendar_unavailable, message="The calendar server gave an unexpected answer."),
        "The server refused the sign-in: check the Apple ID and use an app-specific "
        "password, not your normal Apple password.",
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
    return register_all(path, username, [(source, calendar)], clock)


def register_all(
    path: Path,
    username: str,
    calendars: list[tuple[str, Discovered]],
    clock: Clock | None = None,
) -> bool:
    """Add calendars to the profile in one write with one backup. True when anything changed."""
    profile: dict[str, Any] = {"version": PROFILE_VERSION, "sources": {}}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            profile = cast(dict[str, Any], loaded)
    sources = profile.setdefault("sources", {})
    changed = False
    for source, calendar in calendars:
        entry: dict[str, Any] = {
            "kind": "caldav",
            "url": calendar.url,
            "username": username,
            "credential": credential_name(username),
        }
        if calendar.name.strip():
            entry["label"] = calendar.name.strip()[:80]
        if sources.get(source) != entry:
            sources[source] = entry
            changed = True
    if not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = (clock or SystemClock()).now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        if not backup.exists():
            shutil.copy2(path, backup)
    pending = path.with_suffix(".tmp")
    pending.write_text(_dump(profile), encoding="utf-8")
    pending.replace(path)
    return True


_PRIMARY_NAMES = ("calendar", "home")


def _slug(name: str, room: int = 22) -> str:
    """Lowercase words joined by hyphens, cut at a whole word to fit `room` characters."""
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    kept = ""
    for word in words:
        joined = f"{kept}-{word}" if kept else word
        if len(joined) > room:
            break
        kept = joined
    return kept or (words[0][:room] if words else "other")


def connect_icloud(
    path: Path,
    store: SecretStore,
    username: str,
    *,
    check: Check = verify,
    source: str = ICLOUD_SOURCE,
    clock: Clock | None = None,
) -> list[tuple[str, str]]:
    """Sign in with the saved password and add every iCloud event calendar to the profile.

    The main calendar ("Calendar" or "Home", else the first by name) gets `source`; each other
    one gets `source-<name>`. Returns (source id, calendar name) pairs. Idempotent; a calendar
    the owner does not want can be switched off in the profile (`enabled: false`).
    """
    found = check(username, saved_password(store, username))
    if not found:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE, "iCloud accepted the password but listed no calendars."
        )
    primary = next((c for c in found if c.name.strip().casefold() in _PRIMARY_NAMES), found[0])
    chosen: list[tuple[str, Discovered]] = []
    used = {source}
    limit = source.index(":") + 1 + 32
    for calendar in [primary, *(c for c in found if c is not primary)]:
        sid = source
        if calendar is not primary:
            base = f"{source}-{_slug(calendar.name, limit - len(source) - 4)}"
            sid, number = base, 2
            while sid in used:
                sid = f"{base}-{number}"
                number += 1
        used.add(sid)
        chosen.append((sid, calendar))
    register_all(path, username, chosen, clock)
    return [(sid, calendar.name) for sid, calendar in chosen]


def adopt_old_credential(
    root: Path,
    old_root: Path,
    store: SecretStore,
    *,
    check: Check = verify,
) -> tuple[str, list[tuple[str, str]]]:
    """Move the old harness's saved iCloud password here and connect the calendars (S-17)."""

    saved = icloud_credential(old_root)
    if saved is None:
        raise ChamberlainError(
            ErrorCode.SOURCE_NOT_CONFIGURED,
            "No saved iCloud password was found in the old installation.",
            fix='Run "python -m chamberlain.connectors.caldav.setup credential" instead.',
        )
    username, password = saved
    save_credential(store, username, password, check)
    return username, connect_icloud(profile_path(root), store, username, check=check)


def _dump(profile: dict[str, Any]) -> str:
    return yaml.safe_dump(profile, sort_keys=True)  # pyright: ignore[reportUnknownMemberType]


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
        ("import-old", "move the old harness's saved iCloud password here and connect calendars"),
    ):
        step = sub.add_parser(name, help=text)
        step.add_argument("--apple-id")
        if name == "import-old":
            step.add_argument("--old-data", required=True, help="the old harness's data folder")
        if name == "register":
            step.add_argument("--calendar", help="the calendar's name (default: the only one)")
            step.add_argument("--source", default=ICLOUD_SOURCE)
    args = parser.parse_args(argv)
    try:
        root = data_root_arg(args.data)
        store = default_store(root)
        if args.step == "import-old":
            apple_id, connected = adopt_old_credential(root, Path(args.old_data), store)
            say(f"Moved the saved iCloud password for {apple_id} to the credential store.")
            for sid, name in connected:
                say(f"Connected {name or 'a calendar'} as {sid}.")
            return 0
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
