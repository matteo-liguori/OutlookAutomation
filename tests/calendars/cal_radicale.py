"""A real Radicale server on a free loopback port, started as a subprocess.

A subprocess (not an in-process import) keeps Radicale's own warnings out of the
`filterwarnings = error` gate. One server per test session; one new calendar per test.
"""

import base64
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

USER = "alice"
PASSWORD = "radicale-test-password"
_MKCALENDAR = (
    '<?xml version="1.0"?><C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    "<D:set><D:prop><D:displayname>{name}</D:displayname>"
    "<C:supported-calendar-component-set>"
    '<C:comp name="VEVENT"/></C:supported-calendar-component-set>'
    "</D:prop></D:set></C:mkcalendar>"
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RadicaleServer:
    def __init__(self, folder: Path) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        users = folder / "users"
        users.write_text(f"{USER}:{PASSWORD}\n", encoding="utf-8")
        self._counter = 0
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "radicale",
                "--storage-filesystem-folder",
                str(folder / "collections"),
                "--server-hosts",
                f"127.0.0.1:{self.port}",
                "--auth-type",
                "htpasswd",
                "--auth-htpasswd-filename",
                str(users),
                "--auth-htpasswd-encryption",
                "plain",
                "--logging-level",
                "warning",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait()

    def request(self, method: str, path: str, body: bytes | None = None) -> int:
        token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        request = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/xml"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                return int(reply.status)
        except urllib.error.HTTPError as error:
            error.close()
            return int(error.code)

    def _wait(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                self.request("PROPFIND", "/")
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("Radicale did not start")

    def new_calendar(self) -> str:
        """Create an empty calendar collection and return its URL."""
        self._counter += 1
        name = f"cal{self._counter}"
        status = self.request(
            "MKCALENDAR", f"/{USER}/{name}/", _MKCALENDAR.format(name=name).encode()
        )
        if status != 201:
            raise RuntimeError(f"MKCALENDAR answered {status}")
        return f"{self.base}/{USER}/{name}/"

    def stop(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
