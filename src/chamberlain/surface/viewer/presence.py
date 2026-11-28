"""One viewer per data root: find the running one, or start one that outlives its caller.

A running viewer records its port in `run/viewer.json`; its token is in `run/viewer.token`
(G-18). Opening the viewer means giving the browser the tokened address. The token goes only to
the browser, never to output an agent reads. `open_viewer` reuses a running viewer, or starts
a detached one (`viewer --browser`), which opens the browser itself.

The viewer is one app-style window (Chrome, else Edge, in `--app` mode with its own profile in
`run/viewer-browser`), opened once and reused. The page posts a heartbeat every 15 s; the viewer
writes the time to `run/viewer-page.json`. While a page was seen in the last 45 s nothing new is
opened. A held reply for a live page is queued in `run/viewer-requests.json` and handed to the
page in its next heartbeat reply. Only the launched address carries the token.
"""

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Final

from chamberlain.ports.clock import SystemClock

STATE_FILE: Final = "viewer.json"
TOKEN_FILE: Final = "viewer.token"
PAGE_FILE: Final = "viewer-page.json"
REQUEST_FILE: Final = "viewer-requests.json"
PROFILE_DIR: Final = "viewer-browser"
BIND_ADDRESS: Final = "127.0.0.1"
PAGE_ALIVE_SECONDS: Final = 150
"""Chrome slows a minimised window's timers to about one a minute, so a beat may come late;
the page says when it closes, so this long wait never hides a closed window."""
REQUEST_LIFETIME_SECONDS: Final = 600
_GRANT = re.compile(r"^[a-z]+_[0-9a-f]+$")

Opener = Callable[[str], object]
Spawner = Callable[[Sequence[str]], object]
Now = Callable[[], float]


def system_seconds() -> float:
    """Seconds since the epoch, from the clock port."""
    return SystemClock().now().timestamp()


def record(data_root: Path, port: int) -> None:
    """Called by a viewer once it is bound."""
    run = data_root / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / STATE_FILE).write_text(json.dumps({"port": port, "pid": os.getpid()}), "utf-8")
    for stale in (PAGE_FILE, REQUEST_FILE):  # a new viewer has a new token: old pages are dead
        with contextlib.suppress(OSError):
            (run / stale).unlink()


def forget(data_root: Path) -> None:
    with contextlib.suppress(OSError):
        (data_root / "run" / STATE_FILE).unlink()
    for stale in (PAGE_FILE, REQUEST_FILE):
        with contextlib.suppress(OSError):
            (data_root / "run" / stale).unlink()


def _answers(port: int) -> bool:
    try:
        with socket.create_connection((BIND_ADDRESS, port), timeout=0.5):
            return True
    except OSError:
        return False


def running_url(data_root: Path, *, answers: Callable[[int], bool] | None = None) -> str | None:
    """The tokened address of a viewer already serving this data root, or None."""
    answers = answers or _answers
    run = data_root / "run"
    try:
        port = int(json.loads((run / STATE_FILE).read_text("utf-8"))["port"])
        token = (run / TOKEN_FILE).read_text("utf-8").strip()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not token or not answers(port):
        return None
    return f"http://{BIND_ADDRESS}:{port}/#token={token}"


def _window_python() -> str:
    """`pythonw` beside the current interpreter on Windows, so no console window appears."""
    here = Path(sys.executable)
    quiet = here.with_name("pythonw.exe")
    return str(quiet if sys.platform == "win32" and quiet.is_file() else here)


def _spawn_detached(args: Sequence[str]) -> object:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    if sys.platform == "win32":
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        return subprocess.Popen(
            list(args),
            env=env,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    return subprocess.Popen(
        list(args),
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), "utf-8")
    temporary.replace(path)


def _read_time(path: Path, key: str) -> float | None:
    try:
        value = json.loads(path.read_text("utf-8"))[key]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def mark_page_seen(data_root: Path, *, now: Now = system_seconds) -> None:
    """The page's heartbeat arrived: record when, for any process to read."""
    _write_json(data_root / "run" / PAGE_FILE, {"seen_at": now()})


def page_alive(data_root: Path, *, now: Now = system_seconds) -> bool:
    """True when a viewer page sent a heartbeat recently and has not said it closed."""
    seen = _read_time(data_root / "run" / PAGE_FILE, "seen_at")
    return seen is not None and 0 <= now() - seen <= PAGE_ALIVE_SECONDS


def queue_grant(data_root: Path, grant: str, *, now: Now = system_seconds) -> None:
    """Ask the live page to open `grant` (delivered once, in its next heartbeat reply)."""
    if _GRANT.match(grant):
        _write_json(data_root / "run" / REQUEST_FILE, {"grant": grant, "at": now()})


def take_grant(data_root: Path, *, now: Now = system_seconds) -> str | None:
    """The queued grant, if fresh. The request is cleared, so it is delivered once."""
    path = data_root / "run" / REQUEST_FILE
    try:
        data = json.loads(path.read_text("utf-8"))
        grant, at = data["grant"], float(data["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    with contextlib.suppress(OSError):
        path.unlink()
    fresh = 0 <= now() - at <= REQUEST_LIFETIME_SECONDS
    return grant if fresh and isinstance(grant, str) and _GRANT.match(grant) else None


def heartbeat(
    data_root: Path, *, now: Now = system_seconds, closed: bool = False
) -> dict[str, object]:
    """The viewer's answer to the page's heartbeat: note it, hand over a waiting grant. A
    closing page says so, and the next session opens a window again at once."""
    if closed:
        with contextlib.suppress(OSError):
            (data_root / "run" / PAGE_FILE).unlink()
        return {"grant": None}
    mark_page_seen(data_root, now=now)
    return {"grant": take_grant(data_root, now=now)}


def find_app_browser(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    exists: Callable[[str], bool] = os.path.isfile,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str] | None:
    """The command (before the `--app=` flags) of a Chromium browser, Chrome first, or None."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform == "win32":
        edge = "Microsoft/Edge/Application/msedge.exe"
        chrome = "Google/Chrome/Application/chrome.exe"
        roots = [env.get(n) for n in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for tail in (chrome, edge):
            for root in roots:
                if root and exists(str(PureWindowsPath(root) / tail)):
                    return [str(PureWindowsPath(root) / tail)]
        return None
    if platform == "darwin":
        for name in ("Google Chrome", "Microsoft Edge"):
            if exists(f"/Applications/{name}.app"):
                return ["open", "-na", name, "--args"]
        return None
    for name in ("google-chrome", "chromium", "microsoft-edge"):
        found = which(name)
        if found:
            return [found]
    return None


def installed_browser() -> list[str] | None:
    """The browser on this machine (a seam for tests, which must never open one)."""
    return find_app_browser()


def app_command(launcher: Sequence[str], url: str, data_root: Path) -> list[str]:
    """The full command line for one app-mode window with its own profile."""
    profile = data_root / "run" / PROFILE_DIR
    return [
        *launcher,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def window_opener(
    data_root: Path,
    *,
    spawn: Spawner | None = None,
    find: Callable[[], list[str] | None] | None = None,
    fallback: Opener | None = None,
) -> Opener:
    """An opener that shows an address in the viewer's app window, else in the browser."""

    def open_window(url: str) -> object:
        launcher = (find or installed_browser)()
        if launcher:
            try:
                (spawn or _spawn_detached)(app_command(launcher, url, data_root))
                return True
            except OSError:
                pass
        return (fallback or webbrowser.open)(url)

    return open_window


def open_viewer(
    data_root: Path,
    *,
    opener: Opener | None = None,
    spawn: Spawner = _spawn_detached,
    answers: Callable[[int], bool] | None = None,
    grant: str | None = None,
    now: Now = system_seconds,
) -> str:
    """Show the viewer, on `grant` when given. Returns "already open" (a live page is reused; a
    grant is queued for it), "reused" (a running viewer, new window) or "started" (a new viewer,
    which opens its own window on its front page). Never the token."""
    url = running_url(data_root, answers=answers)
    if url is not None:
        if page_alive(data_root, now=now):
            if grant is not None:
                queue_grant(data_root, grant, now=now)
            return "already open"
        show = opener or window_opener(data_root)
        show(url if grant is None else f"{url}&grant={grant}")
        return "reused"
    spawn(
        [
            _window_python(),
            "-m",
            "chamberlain.surface.cli",
            "--data",
            str(data_root),
            "viewer",
            "--browser",
        ]
    )
    return "started"
