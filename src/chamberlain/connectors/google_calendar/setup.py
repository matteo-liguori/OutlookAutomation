"""Guided setup for the owner's own Google Calendar access.

    python -m chamberlain.connectors.google_calendar.setup client [--from-json FILE]
    python -m chamberlain.connectors.google_calendar.setup link EMAIL
    python -m chamberlain.connectors.google_calendar.setup check
    python -m chamberlain.connectors.google_calendar.setup pages DIR --name APP_NAME

Creating the Cloud project, enabling the Calendar API and making the Desktop client
are console steps; this command handles everything after them. Secrets go to the
operating system credential store (S-17), never to a file or the screen.
"""

import argparse
import getpass
import json
import os
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from chamberlain.connectors.calendar_support.http import (
    HttpTransport,
    TransportFailure,
    UrllibTransport,
)
from chamberlain.connectors.calendar_support.secrets import SecretStore, default_store
from chamberlain.connectors.google_calendar import auth
from chamberlain.connectors.google_calendar.pages import write_pages
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

__all__ = ["check", "link", "main", "read_downloaded", "write_pages"]

CALENDAR_LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
Say = Callable[[str], None]
Authorize = Callable[[SecretStore, str, Callable[[str], None]], None]
ListCalendars = Callable[[SecretStore, str], list[dict[str, str]]]


def read_downloaded(path: Path) -> tuple[str, str]:
    """The JSON Google offers for a client; only a Desktop app client suits the loopback flow."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if "installed" not in data:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            'Create the client with application type "Desktop app".',
            detail="not-a-desktop-app-client",
        )
    return str(data["installed"]["client_id"]), str(data["installed"]["client_secret"])


def list_calendars(
    store: SecretStore,
    email: str,
    clock: Clock | None = None,
    transport: HttpTransport | None = None,
) -> list[dict[str, str]]:
    """The account's calendars with the ids a source binding needs."""
    token = auth.AccessTokens(store, clock or SystemClock())(email)
    try:
        reply = (transport or UrllibTransport()).send(
            "GET",
            CALENDAR_LIST,
            headers={"Authorization": f"Bearer {token}"},
            body=None,
            timeout=30,
        )
    except TransportFailure as error:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE, "Google did not answer.", detail=str(error)
        ) from None
    if reply.status != 200:
        raise ChamberlainError(
            ErrorCode.SOURCE_SIGN_IN_NEEDED
            if reply.status in (401, 403)
            else ErrorCode.SOURCE_UNAVAILABLE,
            "The calendar list could not be read.",
            detail=f"http {reply.status}",
        )
    body = cast(dict[str, Any], json.loads(reply.body))
    return [
        {
            "summary": str(item.get("summary", "")),
            "id": str(item["id"]),
            "role": str(item.get("accessRole", "")),
        }
        for item in body.get("items", [])
    ]


def link(
    store: SecretStore,
    email: str,
    *,
    say: Say = print,
    authorize: Authorize | None = None,
    lister: ListCalendars | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
) -> list[dict[str, str]]:
    """Authorise one account, then prove it by listing its calendars with their binding ids."""

    def show(url: str) -> None:
        say(f"Open this address in the browser signed in as {email} and approve access.")
        say('Google shows an "unverified app" screen for your own app: choose Advanced.')
        say(url)
        try:
            open_browser(url)
        except Exception:
            return

    (authorize or auth.authorize)(store, email, show)
    calendars = (lister or list_calendars)(store, email)
    for item in calendars:
        say(f"  {item['summary']} | {item['role']} | account: {email} | calendar_id: {item['id']}")
    return calendars


def check(store: SecretStore, *, say: Say = print, lister: ListCalendars | None = None) -> bool:
    try:
        auth.load_client(store)
    except ChamberlainError:
        say('No client yet: run the "client" step.')
        return False
    linked = auth.accounts(store)
    if not linked:
        say('No accounts linked yet: run "link EMAIL".')
        return False
    healthy = True
    for email in linked:
        try:
            count = len((lister or list_calendars)(store, email))
            say(f"{email}: ok ({count} calendars)")
        except ChamberlainError as error:
            healthy = False
            say(f"{email}: needs re-linking ({error.message}). Run: link {email}")
    return healthy


def _store(data: str | None) -> SecretStore:
    root = data or os.environ.get("CHAMBERLAIN_DATA")
    if not root:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "Give the data root with --data or CHAMBERLAIN_DATA.",
            field="data",
        )
    return default_store(refuse_inside_checkout(root))


def main(argv: list[str] | None = None, *, say: Say = print) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chamberlain.connectors.google_calendar.setup",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data", help="the data root (default: CHAMBERLAIN_DATA)")
    sub = parser.add_subparsers(dest="step", required=True)
    client = sub.add_parser("client", help="save the Desktop client id and secret")
    client.add_argument("--from-json", help="the JSON file downloaded from the console")
    sub.add_parser("link", help="sign in one Google account and list its calendars").add_argument(
        "email"
    )
    sub.add_parser("check", help="test every linked account")
    pages = sub.add_parser("pages", help="write the homepage and privacy pages")
    pages.add_argument("folder")
    pages.add_argument("--name", required=True)
    pages.add_argument("--source-url", default="")
    args = parser.parse_args(argv)
    try:
        if args.step == "pages":
            for path in write_pages(Path(args.folder), args.name, args.source_url):
                say(f"Wrote {path}")
            say("Host these two pages publicly, then enter their addresses in the console.")
            return 0
        store = _store(args.data)
        if args.step == "client":
            if args.from_json:
                client_id, secret = read_downloaded(Path(args.from_json))
            else:
                client_id, secret = (
                    input("Client ID: "),
                    getpass.getpass("Client secret (hidden): "),
                )
            auth.save_client(store, client_id, secret)
            say("Saved the client to the credential store.")
        elif args.step == "link":
            link(store, args.email, say=say)
            say(f"Linked {args.email}")
        else:
            return 0 if check(store, say=say) else 1
    except (ChamberlainError, auth.AuthorisationFailed, OSError) as error:
        print(f"Setup stopped: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
