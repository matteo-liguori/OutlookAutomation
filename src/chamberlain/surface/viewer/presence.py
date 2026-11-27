"""One viewer per data root: find the running one, or start one that outlives its caller.

A running viewer records its port in `run/viewer.json`; its token is in `run/viewer.token`
(G-18). Opening the viewer means giving the browser the tokened address. The token goes only to
the browser, never to output an agent reads. `open_viewer` reuses a running viewer, or starts
a detached one (`viewer --browser`), which opens the browser itself.
"""

import contextlib
import json
import os
import socket
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

STATE_FILE: Final = "viewer.json"
TOKEN_FILE: Final = "viewer.token"
BIND_ADDRESS: Final = "127.0.0.1"

Opener = Callable[[str], object]
Spawner = Callable[[Sequence[str]], object]


def record(data_root: Path, port: int) -> None:
    """Called by a viewer once it is bound."""
    run = data_root / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / STATE_FILE).write_text(json.dumps({"port": port, "pid": os.getpid()}), "utf-8")


def forget(data_root: Path) -> None:
    with contextlib.suppress(OSError):
        (data_root / "run" / STATE_FILE).unlink()


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


def open_viewer(
    data_root: Path,
    *,
    opener: Opener = webbrowser.open,
    spawn: Spawner = _spawn_detached,
    answers: Callable[[int], bool] | None = None,
) -> str:
    """Show the viewer in the browser. Returns "reused" or "started"; never the token."""
    url = running_url(data_root, answers=answers)
    if url is not None:
        opener(url)
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
