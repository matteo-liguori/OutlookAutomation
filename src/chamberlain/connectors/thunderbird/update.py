"""Update the installed add-on by replacing its file in the Thunderbird profile.

The owner's rule (2026-11-25): an update never goes through the Add-ons Manager, which hung
mid-install; Thunderbird is closed normally, the built package replaces
`<profile>/extensions/<add-on id>.xpi`, and Thunderbird picks up the changed file when it
starts. Only a profile that already has the add-on is touched: a first install still goes
through the Add-ons Manager once (Thunderbird would hold a dropped-in new add-on disabled).
"""

import configparser
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from chamberlain.connectors.thunderbird.setup import EXTENSION_ID, addon_version, build_package
from chamberlain.schema.errors import ChamberlainError, ErrorCode

CLOSE_WAIT_SECONDS: Final = 60


class Desktop(Protocol):
    """The few things the update does to the running Thunderbird."""

    def running(self) -> bool: ...
    def close(self) -> None:
        """Ask Thunderbird to quit normally (never a forced kill)."""
        ...

    def start(self) -> None: ...


def thunderbird_home() -> Path:
    """Where Thunderbird keeps `profiles.ini` on this platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "Thunderbird"
    if system == "Darwin":
        return Path.home() / "Library" / "Thunderbird"
    return Path.home() / ".thunderbird"


def profiles(home: Path) -> list[Path]:
    """Every profile directory `profiles.ini` names."""
    ini = home / "profiles.ini"
    if not ini.is_file():
        return []
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ini, encoding="utf-8")
    found: list[Path] = []
    for section in parser.sections():
        if not section.startswith("Profile") or "Path" not in parser[section]:
            continue
        raw = parser[section]["Path"]
        relative = parser[section].get("IsRelative", "1") == "1"
        found.append((home / raw) if relative else Path(raw))
    return found


@dataclass
class UpdateReport:
    version: str
    updated: list[Path] = field(default_factory=lambda: list[Path]())
    restarted: bool = False

    def lines(self) -> list[str]:
        if not self.updated:
            return [f"Add-on {self.version} built; no profile has it installed yet."]
        lines = [f"Add-on {self.version} placed in {len(self.updated)} profile(s):"]
        lines += [f"  {path}" for path in self.updated]
        lines.append(
            "Thunderbird started again." if self.restarted else "Start Thunderbird to load it."
        )
        return lines


def update_addon(
    bridge_dir: Path,
    *,
    home: Path,
    desktop: Desktop,
    restart: bool,
    build: Callable[[Path], Path] = build_package,
    sleep: Callable[[float], None] = time.sleep,
) -> UpdateReport:
    """Build the package and replace the installed add-on file in every profile that has it."""
    targets = [p / "extensions" / f"{EXTENSION_ID}.xpi" for p in profiles(home)]
    targets = [t for t in targets if t.is_file()]
    package = build(bridge_dir)
    report = UpdateReport(version=addon_version())
    if not targets:
        return report
    was_running = desktop.running()
    if was_running:
        if not restart:
            raise ChamberlainError(
                ErrorCode.SOURCE_BUSY,
                "Thunderbird is running: the add-on file can be replaced only while it is closed.",
                fix="Quit Thunderbird, or pass --restart to have it closed and started again.",
            )
        desktop.close()
        waited = 0.0
        while desktop.running():
            if waited >= CLOSE_WAIT_SECONDS:
                raise ChamberlainError(
                    ErrorCode.SOURCE_BUSY,
                    "Thunderbird did not close within a minute; nothing was replaced.",
                    fix="Answer any question Thunderbird is showing, quit it, and run this again.",
                )
            sleep(1)
            waited += 1
    data = package.read_bytes()
    for target in targets:
        staging = target.with_suffix(".xpi.new")
        staging.write_bytes(data)
        staging.replace(target)
        report.updated.append(target)
    if restart and was_running:
        desktop.start()
        report.restarted = True
    return report


class SystemDesktop:
    """Thunderbird on this computer, through the operating system's own tools."""

    def __init__(self) -> None:
        self._executable: str | None = None

    def running(self) -> bool:
        if sys.platform == "win32":
            done = _run(["tasklist", "/FI", "IMAGENAME eq thunderbird.exe", "/FO", "CSV", "/NH"])
            return "thunderbird.exe" in done.lower()
        return _run(["pgrep", "-x", "thunderbird"]).strip() != ""

    def close(self) -> None:
        if sys.platform == "win32":
            self._executable = (
                _run(_powershell("(Get-Process thunderbird | Select-Object -First 1).Path")).strip()
                or None
            )
            _run(
                _powershell(
                    "Get-Process thunderbird | ForEach-Object { [void]$_.CloseMainWindow() }"
                )
            )
        elif sys.platform == "darwin":
            _run(["osascript", "-e", 'quit app "Thunderbird"'])
        else:
            _run(["pkill", "-TERM", "-x", "thunderbird"])

    def start(self) -> None:
        if sys.platform == "win32":
            executable = self._executable or r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe"
            subprocess.Popen([executable], close_fds=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Thunderbird"])
        else:
            subprocess.Popen(["thunderbird"])


def _powershell(command: str) -> Sequence[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]


def _run(args: Sequence[str]) -> str:
    done = subprocess.run(list(args), capture_output=True, text=True, check=False)
    return done.stdout


__all__ = [
    "Desktop",
    "SystemDesktop",
    "UpdateReport",
    "profiles",
    "thunderbird_home",
    "update_addon",
]
