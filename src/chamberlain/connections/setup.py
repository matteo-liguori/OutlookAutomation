"""Dispatch setup choices, progress checks and the existing provider commands."""

import argparse
from contextlib import redirect_stdout
from pathlib import Path
from typing import TextIO

from chamberlain.connections.build import BRIDGE_DIR_NAME
from chamberlain.connections.onboarding import SetupProvider, setup_select, setup_status
from chamberlain.connectors.caldav import setup as icloud_setup
from chamberlain.connectors.google_calendar import setup as google_setup
from chamberlain.connectors.thunderbird import setup as thunderbird_setup
from chamberlain.schema.errors import ChamberlainError


def _onboarding(command: str, argv: list[str], data_root: Path, out: TextIO) -> int:
    parser = argparse.ArgumentParser(prog=f"chamberlain setup {command}")
    if command == "select":
        parser.add_argument("providers", nargs="+", type=SetupProvider, choices=list(SetupProvider))
        parser.add_argument("--google-account", metavar="EMAIL")
        parser.add_argument("--apple-id", metavar="EMAIL")
    with redirect_stdout(out):
        args = parser.parse_args(argv)
    try:
        state = (
            setup_select(
                data_root,
                args.providers,
                google_account=args.google_account,
                apple_id=args.apple_id,
            )
            if command == "select"
            else setup_status(data_root)
        )
    except ChamberlainError as error:
        print(error.message, file=out)
        if error.fix:
            print(error.fix, file=out)
        return 2
    out.write(state.model_dump_json(indent=2, exclude_none=True) + "\n")
    return 0


def provider_setup(provider: str | None, argv: list[str], data_root: Path, out: TextIO) -> int:
    """Pass existing provider flags through, using the harness root for their defaults."""
    try:
        if provider is None or provider in ("select", "status"):
            return _onboarding(provider or "status", argv, data_root, out)
        if provider == "thunderbird":
            with redirect_stdout(out):
                return thunderbird_setup.main(
                    ["--bridge-dir", str(data_root / BRIDGE_DIR_NAME), *argv]
                )
        if provider == "google":
            setup = google_setup.main
        elif provider == "icloud":
            setup = icloud_setup.main
        else:
            print(f"Unknown provider: {provider}. Choose thunderbird, google or icloud.", file=out)
            return 2
        # Regular messages go to the caller, while input and getpass use the actual terminal.
        return setup(["--data", str(data_root), *argv], say=lambda text: print(text, file=out))
    except SystemExit as error:
        # A provider's ArgumentParser exits on help or malformed arguments.
        return error.code if isinstance(error.code, int) else 1
