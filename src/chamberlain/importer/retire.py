"""Retire v1's running parts: refuse while its processes run, then archive its Thunderbird
add-on and native host (kept for rollback).

Everything that touches the machine is injected (process list, registry, Thunderbird, paths), so
the tests never touch the real system. Nothing here acts unless `apply` is true.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from chamberlain.connectors.thunderbird.update import (
    CLOSE_WAIT_SECONDS,
    Desktop,
    profiles,
    thunderbird_home,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode

V1_ADDON_FILE: Final = "mail-pilot@outlookautomation.local.xpi"
V1_HOST_NAME: Final = "outlookautomation_thunderbird_pilot"
V1_HOST_KEY: Final = rf"HKCU\Software\Mozilla\NativeMessagingHosts\{V1_HOST_NAME}"

_V1_COMMAND: Final = re.compile(
    r"-m\s+monitor\b|monitor\.ps1|" + V1_HOST_NAME + r"|mail-pilot@outlookautomation",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RunningProcess:
    pid: int
    name: str
    command: str


def system_processes() -> list[RunningProcess]:
    """Every process with its command line, through the operating system's own tools."""
    found: list[RunningProcess] = []
    if sys.platform == "win32":
        script = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
        )
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            loaded: Any = json.loads(done.stdout or "[]")
        except ValueError:
            return []
        rows = cast(list[Any], loaded if isinstance(loaded, list) else [loaded])
        for item in rows:
            row = cast(dict[str, Any], item) if isinstance(item, dict) else {}
            found.append(
                RunningProcess(
                    int(row.get("ProcessId") or 0),
                    str(row.get("Name") or ""),
                    str(row.get("CommandLine") or ""),
                )
            )
        return found
    done = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False)
    for line in done.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            found.append(RunningProcess(int(pid), command.split(" ")[0], command.strip()))
    return found


def _norm(text: str) -> str:
    return text.replace("/", "\\").casefold()


def v1_processes(
    processes: Sequence[RunningProcess], old_checkout: Path | None
) -> list[RunningProcess]:
    """Processes that run v1: from its checkout, `-m monitor`, or its native host.

    This program, its parent shell and anything named Chamberlain are never v1.
    """
    own = {os.getpid(), os.getppid()}
    checkout = _norm(str(old_checkout)) if old_checkout is not None else None
    found: list[RunningProcess] = []
    for process in processes:
        command = process.command
        if process.pid in own or "chamberlain" in command.casefold() or not command:
            continue
        if (checkout is not None and checkout in _norm(command)) or _V1_COMMAND.search(command):
            found.append(process)
    return found


def refuse_while_running(
    processes: Sequence[RunningProcess], old_checkout: Path | None = None
) -> None:
    running = v1_processes(processes, old_checkout)
    if running:
        listing = "; ".join(f"{p.name} (pid {p.pid})" for p in running[:6])
        raise ChamberlainError(
            ErrorCode.SOURCE_BUSY,
            f"v1 is still running: {listing}. Two systems must not run at once.",
            fix="Close the v1 dashboard, native host and any `-m monitor` run (stop the pids "
            "above or end the Thunderbird add-on's host by closing Thunderbird), then run "
            "prepare again.",
        )


class NativeHostRegistry(Protocol):
    """Where v1's native-messaging host is registered on this platform."""

    def export_to(self, archive: Path) -> Path | None:
        """Save the registration into `archive`; None when it is not registered."""
        ...

    def remove(self) -> None: ...


class WindowsNativeHost:
    """The HKCU key, saved with `reg export` and removed with `reg delete`."""

    def __init__(self, run: Callable[[list[str]], int] | None = None) -> None:
        self._run = run or _reg

    def _exists(self) -> bool:
        return self._run(["reg", "query", V1_HOST_KEY]) == 0

    def export_to(self, archive: Path) -> Path | None:
        if not self._exists():
            return None
        target = archive / "v1-native-host.reg"
        if self._run(["reg", "export", V1_HOST_KEY, str(target), "/y"]) != 0:
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The v1 native-host registry key could not be saved.",
                fix="Nothing was removed. Export it by hand, then remove it.",
            )
        return target

    def remove(self) -> None:
        if self._exists() and self._run(["reg", "delete", V1_HOST_KEY, "/f"]) != 0:
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE, "The v1 native-host registry key was not removed."
            )


class ManifestFileHost:
    """Mac and Linux: the host is a manifest file; archiving moves it."""

    def __init__(self, manifest: Path) -> None:
        self.manifest = manifest

    def export_to(self, archive: Path) -> Path | None:
        if not self.manifest.is_file():
            return None
        target = archive / f"v1-native-host-{self.manifest.name}"
        shutil.copy2(self.manifest, target)
        return target

    def remove(self) -> None:
        self.manifest.unlink(missing_ok=True)


def system_native_host() -> NativeHostRegistry:
    if sys.platform == "win32":
        return WindowsNativeHost()
    base = (
        Path.home() / "Library" / "Application Support" / "Mozilla" / "NativeMessagingHosts"
        if sys.platform == "darwin"
        else Path.home() / ".mozilla" / "native-messaging-hosts"
    )
    return ManifestFileHost(base / f"{V1_HOST_NAME}.json")


def _reg(args: list[str]) -> int:
    return subprocess.run(args, capture_output=True, check=False).returncode


@dataclass
class RetireResult:
    applied: bool
    steps: list[str] = field(default_factory=lambda: list[str]())
    archive: Path | None = None


def retire_v1_addon(
    archive: Path,
    *,
    apply: bool,
    desktop: Desktop,
    host: NativeHostRegistry,
    home: Path | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RetireResult:
    """Archive v1's Thunderbird add-on file and native host. Dry run unless `apply`.

    With `apply`, Thunderbird is closed first (a normal close, never a kill), the registration
    is exported then removed, and each profile's v1 xpi is moved into `archive`. Safe to repeat.
    """
    base = home if home is not None else thunderbird_home()
    xpis = [p / "extensions" / V1_ADDON_FILE for p in profiles(base)]
    xpis = [x for x in xpis if x.is_file()]
    result = RetireResult(apply, archive=archive)
    result.steps = [f"move {x} to {archive / ('v1-addon-' + V1_ADDON_FILE)}" for x in xpis]
    result.steps.append(f"export then remove the v1 native host ({V1_HOST_NAME})")
    if not apply:
        return result
    if xpis and desktop.running():
        _close(desktop, sleep)
    archive.mkdir(parents=True, exist_ok=True)
    host.export_to(archive)
    host.remove()
    for number, xpi in enumerate(xpis):
        name = "v1-addon-" + V1_ADDON_FILE
        target = archive / (name if number == 0 else f"{number}-{name}")
        shutil.move(str(xpi), target)
    return result


def _close(desktop: Desktop, sleep: Callable[[float], None] | None) -> None:
    pause = sleep or time.sleep
    desktop.close()
    waited = 0.0
    while desktop.running():
        if waited >= CLOSE_WAIT_SECONDS:
            raise ChamberlainError(
                ErrorCode.SOURCE_BUSY,
                "Thunderbird did not close within a minute; nothing was archived.",
                fix="Answer any question Thunderbird is showing, quit it, and run this again.",
            )
        pause(1)
        waited += 1
