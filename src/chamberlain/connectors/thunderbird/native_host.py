"""The native messaging host: relays authenticated local clients to the add-on.

Contract: docs/contracts/12-bridge-v2.md sections 3, 4, 7 and 10. Thunderbird
starts this process and talks to it over stdin and stdout. Local clients
connect over loopback TCP and must present the session token. The host checks
the handshake, refuses commands outside the closed list, relays frames and
never interprets results. A dropped client or a bad frame ends only that
client's connection; a lost response is never retried.
"""

import contextlib
import hmac
import importlib
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from chamberlain.connectors.thunderbird import frames, protocol
from chamberlain.connectors.thunderbird.frames import Readable, Writable
from chamberlain.connectors.thunderbird.protocol import (
    PROTOCOL_VERSION,
    AddonHello,
    BridgeError,
    BridgeErrorCode,
    ChunkFrame,
    ClientHello,
    Command,
    Refusal,
    RefusalCode,
    RequestFrame,
    ResponseFrame,
    Welcome,
)
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

HOST_VERSION: Final = "2.0.0"
RUNTIME_FILE: Final = "runtime.json"
FAULT_FILE: Final = "livetest-fault.json"
"""Test hook: `<data root>/run/livetest-fault.json` (see `LiveFault`)."""
FAULT_MAX_AGE_SECONDS: Final = 600
FAULT_DROP_RESPONSE: Final = "drop_response_after_dispatch"
LIVE_TEST_MARKER: Final = "CHAMBERLAIN LIVE TEST"


@dataclass(frozen=True)
class Deadlines:
    """Nested deadlines in seconds: add-on, then host, then client (contract section 10)."""

    addon_read: float = 30.0
    addon_write: float = 45.0
    host_read: float = 38.0
    host_write: float = 53.0
    client_read: float = 43.0
    client_write: float = 58.0

    def __post_init__(self) -> None:
        ordered = (
            self.addon_read < self.host_read < self.client_read
            and self.addon_write < self.host_write < self.client_write
        )
        if not ordered:
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                "Bridge deadlines must nest: add-on, then host, then client.",
            )

    def scaled(self, factor: float) -> "Deadlines":
        return Deadlines(
            self.addon_read * factor,
            self.addon_write * factor,
            self.host_read * factor,
            self.host_write * factor,
            self.client_read * factor,
            self.client_write * factor,
        )


class LiveFault:
    """A one-shot, safe fault for `chamberlain live-test`: lose one response after dispatch.

    Active only while a fault file names a run id and the fault. It fires for a `send`
    request whose subject contains `CHAMBERLAIN LIVE TEST <run id>`, so no other request can
    ever be touched. A file older than ten minutes is ignored. The file is claimed by an
    atomic rename before the request is forwarded, so it is used at most once, and it is
    deleted. The request is still sent to the add-on and executed; only the response is not
    relayed, and the client connection is closed (the client reports an uncertain write).
    """

    def __init__(self, path: Path, now_seconds: Callable[[], float] | None = None) -> None:
        self._path = path
        self._now = now_seconds or (lambda: time.time_ns() / 1e9)

    def claim(self, request: RequestFrame) -> bool:
        """True when this request must have its response dropped. Consumes the file."""
        if request.cmd != Command.SEND.value:
            return False
        try:
            if self._now() - self._path.stat().st_mtime > FAULT_MAX_AGE_SECONDS:
                return False
            loaded: object = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(loaded, dict):
            return False
        spec = cast("dict[str, object]", loaded)
        run_id = spec.get("run_id")
        subject = request.args.get("subject")
        if (
            not isinstance(run_id, str)
            or not run_id
            or spec.get("fault") != FAULT_DROP_RESPONSE
            or not isinstance(subject, str)
            or f"{LIVE_TEST_MARKER} {run_id}" not in subject
        ):
            return False
        taken = self._path.with_name(f"{self._path.name}.claimed-{os.getpid()}")
        try:
            os.replace(self._path, taken)
        except OSError:
            return False
        with contextlib.suppress(OSError):
            taken.unlink()
        return True


def private_directory(path: Path, *, restrict: Callable[[Path], None] | None = None) -> None:
    """Create a directory only the current user can use (Windows needs an explicit ACL)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    (restrict or _restrict_to_user)(path)


def _restrict_to_user(path: Path) -> None:
    if sys.platform != "win32":
        path.chmod(0o700)
        return
    user = os.environ.get("USERNAME", "")
    if not user:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID, "Cannot determine the Windows user for the bridge directory."
        )
    domain = os.environ.get("USERDOMAIN", "")
    account = f"{domain}\\{user}" if domain else user
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "Could not restrict the bridge directory to the current user.",
            detail=result.stderr[:300],
        )


def write_runtime(bridge_dir: Path, *, port: int, token: str, pid: int) -> Path:
    """Publish how to reach the host; replaced atomically."""
    target = bridge_dir / RUNTIME_FILE
    pending = bridge_dir / f"runtime-{secrets.token_hex(6)}.json"
    body = json.dumps({"pid": pid, "port": port, "token": token, "protocol": PROTOCOL_VERSION})
    pending.write_text(body, encoding="utf-8")
    pending.replace(target)
    return target


class SocketStream:
    """A connected socket as `Readable` and `Writable`."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def read(self, size: int, /) -> bytes:
        return self._connection.recv(size)

    def write(self, data: bytes | memoryview, /) -> int:
        self._connection.sendall(data)
        return len(data)

    def flush(self) -> None:
        return None


class FdReader:
    """Raw reads from a file descriptor; never holds Python's buffered-stdin lock."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def read(self, size: int, /) -> bytes:
        return os.read(self._fd, size)


class FdWriter:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def write(self, data: bytes | memoryview, /) -> int:
        return os.write(self._fd, data)

    def flush(self) -> None:
        return None


_Pending = queue.Queue[ChunkFrame | ResponseFrame | None]


class NativeHost:
    """One add-on connection and any number of short-lived local clients."""

    def __init__(
        self,
        native_in: Readable,
        native_out: Writable,
        listener: socket.socket,
        *,
        token: str,
        deadlines: Deadlines | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        fault_file: Path | None = None,
    ) -> None:
        self._native_in = native_in
        self._native_out = native_out
        self._listener = listener
        self._token = token
        self._deadlines = deadlines or Deadlines()
        self._monotonic = monotonic
        self._fault = None if fault_file is None else LiveFault(fault_file)
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._addon: AddonHello | None = None
        self._pending: dict[str, _Pending] = {}
        self._stopped = threading.Event()
        self._threads: list[threading.Thread] = []
        self._connections: set[socket.socket] = set()

    # --- lifecycle ----------------------------------------------------------------

    @property
    def port(self) -> int:
        return int(self._listener.getsockname()[1])

    def start(self) -> None:
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        for target, name in ((self._pump_native, "native-in"), (self._accept_loop, "accept")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def wait(self) -> None:
        """Block until the add-on connection ends (the process then exits)."""
        self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()
        with self._state_lock:
            connections = list(self._connections)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.close()
        with contextlib.suppress(OSError):
            self._listener.close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=3)
        self._fail_pending()

    @property
    def addon_connected(self) -> bool:
        with self._state_lock:
            return self._addon is not None and not self._stopped.is_set()

    # --- add-on side --------------------------------------------------------------

    def _pump_native(self) -> None:
        try:
            while not self._stopped.is_set():
                raw = frames.read_frame(self._native_in, frames.FROM_ADDON_LIMIT)
                if raw is None:
                    break
                try:
                    frame = protocol.parse_frame(raw)
                except ChamberlainError:
                    continue
                self._route_from_addon(frame)
        except ChamberlainError:
            pass
        finally:
            self._stopped.set()
            self._fail_pending()

    def _route_from_addon(self, frame: object) -> None:
        if isinstance(frame, AddonHello):
            with self._state_lock:
                self._addon = frame
        elif isinstance(frame, (ChunkFrame, ResponseFrame)):
            with self._state_lock:
                pending = self._pending.get(frame.id)
            if pending is not None:
                pending.put(frame)
        elif isinstance(frame, Refusal):
            with self._state_lock:
                pending = self._pending.get(frame.id)
            if pending is not None:
                pending.put(None)

    def _fail_pending(self) -> None:
        with self._state_lock:
            queues = list(self._pending.values())
        for pending in queues:
            pending.put(None)

    def _to_addon(self, frame: protocol.RequestFrame | ChunkFrame) -> None:
        body = protocol.dump_frame(frame)
        with self._write_lock:
            frames.write_frame(self._native_out, body, frames.TO_ADDON_LIMIT)

    # --- client side --------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._state_lock:
                self._connections.add(connection)
            thread = threading.Thread(target=self._serve_client, args=(connection,), daemon=True)
            thread.start()

    def _serve_client(self, connection: socket.socket) -> None:
        stream = SocketStream(connection)
        try:
            connection.settimeout(10)
            self._converse(stream, connection)
        except (ChamberlainError, OSError):
            pass  # a bad frame or a dropped client ends only this connection
        finally:
            with self._state_lock:
                self._connections.discard(connection)
            with contextlib.suppress(OSError):
                connection.close()

    def _send(self, stream: Writable, frame: Model) -> None:
        frames.write_frame(stream, protocol.dump_frame(frame), frames.LOCAL_LIMIT)

    def _refuse(self, stream: Writable, id_: str, code: RefusalCode, message: str) -> None:
        refusal = Refusal(v=PROTOCOL_VERSION, id=id_, code=code, message=message)
        with contextlib.suppress(ChamberlainError):
            self._send(stream, refusal)

    def _converse(self, stream: SocketStream, connection: socket.socket) -> None:
        raw = frames.read_frame(stream, frames.LOCAL_LIMIT)
        if raw is None:
            return
        try:
            hello = protocol.parse_frame(raw)
        except ChamberlainError:
            self._refuse(stream, "bad-hello-000", RefusalCode.BAD_HELLO, "Expected a client hello.")
            return
        if not isinstance(hello, ClientHello):
            self._refuse(stream, "bad-hello-000", RefusalCode.BAD_HELLO, "Expected a client hello.")
            return
        if not hmac.compare_digest(hello.token.encode(), self._token.encode()):
            self._refuse(stream, hello.id, RefusalCode.UNAUTHENTICATED, "Bad session token.")
            return
        if hello.v != PROTOCOL_VERSION:
            self._refuse(stream, hello.id, RefusalCode.VERSION_MISMATCH, "Unsupported version.")
            return
        with self._state_lock:
            addon = self._addon
        if addon is None or self._stopped.is_set():
            self._refuse(stream, hello.id, RefusalCode.ADDON_NOT_CONNECTED, "Add-on not connected.")
            return
        if addon.v != PROTOCOL_VERSION:
            self._refuse(stream, hello.id, RefusalCode.VERSION_MISMATCH, "Add-on version differs.")
            return
        if not set(hello.require) <= set(addon.capabilities):
            self._refuse(stream, hello.id, RefusalCode.CAPABILITY_MISSING, "Add-on lacks features.")
            return
        self._send(
            stream,
            Welcome(
                v=PROTOCOL_VERSION,
                id=hello.id,
                host_version=HOST_VERSION,
                addon_version=addon.addon_version,
                capabilities=addon.capabilities,
            ),
        )
        connection.settimeout(self._deadlines.client_write)
        raw_request = frames.read_frame(stream, frames.LOCAL_LIMIT)
        if raw_request is None:
            return
        self._relay(stream, raw_request)

    def _relay(self, stream: SocketStream, raw_request: bytes) -> None:
        request = protocol.parse_frame(raw_request)
        if not isinstance(request, RequestFrame):
            return
        if request.v != PROTOCOL_VERSION:
            self._refuse(stream, request.id, RefusalCode.VERSION_MISMATCH, "Unsupported version.")
            return
        is_write = request.cmd in {c.value for c in protocol.WRITE_COMMANDS}
        if request.cmd not in {c.value for c in Command}:
            self._answer_error(
                stream, request, BridgeErrorCode.UNKNOWN_COMMAND, "No such command.", "none"
            )
            return
        pending: _Pending = queue.Queue()
        with self._state_lock:
            if request.id in self._pending:
                self._refuse(stream, request.id, RefusalCode.DUPLICATE_ID, "Request ID in use.")
                return
            self._pending[request.id] = pending
        drop = self._fault is not None and self._fault.claim(request)
        try:
            self._forward(stream, request)
            self._wait_for_response(stream, request, pending, is_write, drop)
        finally:
            with self._state_lock:
                self._pending.pop(request.id, None)

    def _forward(self, stream: SocketStream, request: RequestFrame) -> None:
        self._to_addon(request)
        total = 0
        attachments = request.args.get("attachments")
        if isinstance(attachments, list):
            for spec in attachments:
                count = spec.get("chunks") if isinstance(spec, dict) else None
                if isinstance(count, int):
                    total += count
        for _ in range(total):
            raw = frames.read_frame(stream, frames.LOCAL_LIMIT)
            if raw is None:
                raise ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, "Client left mid-transfer.")
            chunk = protocol.parse_frame(raw)
            if not isinstance(chunk, ChunkFrame) or chunk.id != request.id:
                raise ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, "Unexpected frame.")
            self._to_addon(chunk)

    def _wait_for_response(
        self,
        stream: SocketStream,
        request: RequestFrame,
        pending: _Pending,
        is_write: bool,
        drop: bool = False,
    ) -> None:
        limit = self._deadlines.host_write if is_write else self._deadlines.host_read
        deadline = self._monotonic() + limit
        mutation = "possible" if is_write else "none"
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                self._answer_error(
                    stream, request, BridgeErrorCode.TIMEOUT, "Bridge response uncertain.", mutation
                )
                return
            try:
                item = pending.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            if item is None:
                self._answer_error(
                    stream,
                    request,
                    BridgeErrorCode.INTERNAL,
                    "Bridge response uncertain.",
                    mutation,
                )
                return
            if isinstance(item, ResponseFrame) and drop:
                return  # the write ran; the answer is lost and the connection closes
            self._send(stream, item)
            if isinstance(item, ResponseFrame):
                return

    def _answer_error(
        self,
        stream: Writable,
        request: RequestFrame,
        code: BridgeErrorCode,
        message: str,
        mutation: str,
    ) -> None:
        error = BridgeError(
            code=code,
            message=message,
            mutation="possible" if mutation == "possible" else "none",
        )
        response = ResponseFrame(
            v=PROTOCOL_VERSION, id=request.id, cmd=request.cmd, ok=False, error=error
        )
        with contextlib.suppress(ChamberlainError, OSError):
            self._send(stream, response)


def fault_file_for(bridge_dir: Path) -> Path | None:
    """`<data root>/run/livetest-fault.json`, only in the default layout `<data root>/bridge`."""
    if bridge_dir.name != "bridge":
        return None
    return bridge_dir.parent / "run" / FAULT_FILE


def serve(bridge_dir: Path) -> int:
    """Entry point Thunderbird starts: relay between stdio and loopback clients."""
    if sys.platform == "win32":
        msvcrt = importlib.import_module("msvcrt")  # Windows only: binary stdio
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    refuse_inside_checkout(bridge_dir)
    private_directory(bridge_dir)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    token = secrets.token_hex(32)
    native_out = FdWriter(os.dup(sys.stdout.fileno()))
    host = NativeHost(
        FdReader(sys.stdin.fileno()),
        native_out,
        listener,
        token=token,
        fault_file=fault_file_for(bridge_dir),
    )
    runtime = write_runtime(bridge_dir, port=host.port, token=token, pid=os.getpid())
    host.start()
    try:
        host.wait()
    finally:
        host.stop()
        with contextlib.suppress(OSError, ValueError, KeyError):
            if json.loads(runtime.read_text(encoding="utf-8"))["pid"] == os.getpid():
                runtime.unlink()
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        sys.stderr.write("usage: native_host <bridge directory>\n")
        return 2
    return serve(Path(arguments[0]))


if __name__ == "__main__":
    raise SystemExit(main())
