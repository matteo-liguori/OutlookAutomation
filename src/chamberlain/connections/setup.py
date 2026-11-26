"""Dispatch the three provider setup commands without taking over terminal input."""

from contextlib import redirect_stdout
from pathlib import Path
from typing import TextIO

from chamberlain.connections.build import BRIDGE_DIR_NAME
from chamberlain.connectors.caldav import setup as icloud_setup
from chamberlain.connectors.google_calendar import setup as google_setup
from chamberlain.connectors.thunderbird import setup as thunderbird_setup


def provider_setup(provider: str, argv: list[str], data_root: Path, out: TextIO) -> int:
    """Pass existing provider flags through, using the harness root for their defaults."""
    try:
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
