"""The local bridge client: one authenticated connection per request.

Contract: docs/contracts/12-bridge-v2.md sections 3, 4, 7 and 10. The client
opens a connection through an injectable `Connector`, does the handshake,
sends one request (and its attachment chunks), reads the response and its
chunks, and reports failures as `BridgeFailure`, which says whether the
request may have reached the add-on. Nothing here retries.
"""

import base64
import contextlib
import json
import secrets
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, cast

from pydantic import JsonValue

from chamberlain.connectors.thunderbird import frames, protocol
from chamberlain.connectors.thunderbird.native_host import RUNTIME_FILE, Deadlines
from chamberlain.connectors.thunderbird.protocol import (
    PROTOCOL_VERSION,
    REQUIRED_CAPABILITIES,
    BridgeErrorCode,
    ChunkFrame,
    ClientHello,
    Command,
    Refusal,
    RequestFrame,
    ResponseFrame,
    Welcome,
)
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

CLIENT_VERSION: Final = "2.0.0"
UPDATE_FIX: Final = (
    "Install the current Chamberlain add-on in Thunderbird, restart Thunderbird, "
    "and keep it running."
)

_CODE_MAP: Final[dict[BridgeErrorCode, ErrorCode]] = {
    BridgeErrorCode.BAD_REQUEST: ErrorCode.REQUEST_INVALID,
    BridgeErrorCode.UNKNOWN_COMMAND: ErrorCode.CAPABILITY_UNSUPPORTED,
    BridgeErrorCode.ACCOUNT_MISMATCH: ErrorCode.SOURCE_NOT_CONFIGURED,
    BridgeErrorCode.NOT_FOUND: ErrorCode.REQUEST_UNKNOWN_HANDLE,
    BridgeErrorCode.AMBIGUOUS: ErrorCode.REMOTE_AMBIGUOUS,
    BridgeErrorCode.CHANGED: ErrorCode.REMOTE_CHANGED,
    BridgeErrorCode.BUSY: ErrorCode.SOURCE_BUSY,
    BridgeErrorCode.TIMEOUT: ErrorCode.SOURCE_UNAVAILABLE,
    BridgeErrorCode.OFFLINE: ErrorCode.SOURCE_UNAVAILABLE,
    BridgeErrorCode.NOT_VERIFIED: ErrorCode.SOURCE_UNAVAILABLE,
    BridgeErrorCode.TOO_LARGE: ErrorCode.REQUEST_TOO_LARGE,
    BridgeErrorCode.HASH_MISMATCH: ErrorCode.ARTIFACT_HASH_MISMATCH,
    BridgeErrorCode.UNSUPPORTED: ErrorCode.CAPABILITY_UNSUPPORTED,
    BridgeErrorCode.INTERNAL: ErrorCode.SOURCE_UNAVAILABLE,
}


class BridgeFailure(ChamberlainError):
    """A bridge failure; `sent` is True when the request may have reached the add-on."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        sent: bool,
        mutation: str = "none",
        bridge_code: BridgeErrorCode | None = None,
        fix: str | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(code, message, fix=fix, detail=detail)
        self.sent = sent
        self.mutation = mutation
        self.bridge_code = bridge_code

    @property
    def proven_not_applied(self) -> bool:
        """The connector may call a write `not_applied` only on this (P-10)."""
        return not self.sent or (self.bridge_code is not None and self.mutation == "none")


class Connection(Protocol):
    def read(self, size: int, /) -> bytes: ...

    def write(self, data: bytes | memoryview, /) -> int | None: ...

    def flush(self) -> None: ...

    def settimeout(self, seconds: float) -> None: ...

    def close(self) -> None: ...


class Connector(Protocol):
    def open(self, timeout: float) -> Connection: ...

    def token(self) -> str: ...


class SocketConnection:
    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def read(self, size: int, /) -> bytes:
        return self._connection.recv(size)

    def write(self, data: bytes | memoryview, /) -> int:
        self._connection.sendall(data)
        return len(data)

    def flush(self) -> None:
        return None

    def settimeout(self, seconds: float) -> None:
        self._connection.settimeout(seconds)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._connection.close()


class RuntimeConnector:
    """Find the host through `runtime.json` in the bridge directory and connect to loopback."""

    def __init__(self, bridge_dir: Path) -> None:
        self._runtime = bridge_dir / RUNTIME_FILE

    def _state(self) -> dict[str, JsonValue]:
        try:
            state: object = json.loads(self._runtime.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The Thunderbird bridge is not running.",
                sent=False,
                fix="Start Thunderbird with the Chamberlain add-on installed.",
            ) from None
        if not isinstance(state, dict):
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE, "The bridge state is unreadable.", sent=False
            )
        return cast(dict[str, JsonValue], state)

    def token(self) -> str:
        return str(self._state().get("token", ""))

    def open(self, timeout: float) -> Connection:
        state = self._state()
        port = state.get("port")
        if not isinstance(port, int):
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE, "The bridge state is unreadable.", sent=False
            )
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        except OSError as error:
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The Thunderbird bridge did not answer.",
                sent=False,
                fix="Start Thunderbird and keep it running.",
                detail=str(error),
            ) from None
        return SocketConnection(connection)


@dataclass
class Reply:
    result: Model
    chunks: dict[str, list[ChunkFrame]] = field(default_factory=lambda: {})

    def assembled(self, name: str, *, expected: int) -> bytes:
        """The bytes of one chunked transfer, every chunk hash verified (P-8)."""
        parts = self.chunks.get(name, [])
        if len(parts) != expected:
            raise _corrupt("chunk count")
        out = bytearray()
        for index, chunk in enumerate(parts):
            try:
                raw = base64.b64decode(chunk.data, validate=True)
            except ValueError:
                raise _corrupt("chunk encoding") from None
            if (
                chunk.index != index
                or sha256_bytes(raw) != chunk.sha256
                or len(raw) > frames.CHUNK_BYTES
            ):
                raise _corrupt("chunk hash")
            out.extend(raw)
        return bytes(out)


def _corrupt(what: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.ARTIFACT_HASH_MISMATCH,
        "The attachment bytes did not match their hash.",
        detail=what,
    )


def split_chunks(data: bytes) -> list[bytes]:
    if not data:
        return []
    size = frames.CHUNK_BYTES
    return [data[start : start + size] for start in range(0, len(data), size)]


class BridgeClient:
    def __init__(
        self,
        connector: Connector,
        deadlines: Deadlines | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self._connector = connector
        self.deadlines = deadlines or Deadlines()
        self._monotonic = monotonic
        self._new_id = new_id

    # --- handshake ----------------------------------------------------------------

    def hello(self) -> Welcome:
        """Connect, handshake without extra requirements, and say what the add-on offers."""
        connection = self._connector.open(timeout=5)
        try:
            return self._handshake(connection)
        finally:
            connection.close()

    def _handshake(self, connection: Connection, require: Iterable[str] = ()) -> Welcome:
        needed = {c.value for c in REQUIRED_CAPABILITIES} | set(require)
        try:
            hello = ClientHello(
                v=PROTOCOL_VERSION,
                id=self._new_id(),
                token=self._connector.token(),
                client_version=CLIENT_VERSION,
                require=tuple(sorted(needed)),
            )
            connection.settimeout(10)
            frames.write_frame(connection, protocol.dump_frame(hello), frames.LOCAL_LIMIT)
            raw = frames.read_frame(connection, frames.LOCAL_LIMIT)
        except BridgeFailure:
            raise
        except ChamberlainError as error:
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The Thunderbird bridge handshake failed.",
                sent=False,
                detail=error.detail,
            ) from None
        if raw is None:
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE, "The bridge closed the connection.", sent=False
            )
        try:
            answer = protocol.parse_frame(raw)
        except ChamberlainError:
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The bridge answered unexpectedly.",
                sent=False,
                fix=UPDATE_FIX,
            ) from None
        if isinstance(answer, Refusal):
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE,
                f"The Thunderbird bridge refused: {answer.code.value}.",
                sent=False,
                fix=UPDATE_FIX,
                detail=answer.message,
            )
        if not isinstance(answer, Welcome):
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE, "The bridge answered unexpectedly.", sent=False
            )
        missing = needed - set(answer.capabilities)
        if answer.v != PROTOCOL_VERSION or missing:
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The Thunderbird add-on is out of date.",
                sent=False,
                fix=UPDATE_FIX,
                detail=f"version {answer.v}, missing {sorted(missing)}",
            )
        return answer

    # --- one request --------------------------------------------------------------

    def call(
        self,
        cmd: Command,
        args: Model,
        *,
        payloads: Mapping[str, bytes] | None = None,
        require: Iterable[str] = (),
    ) -> Reply:
        """Send one request. `payloads` maps each attachment key to its bytes.

        `require` names optional add-on capabilities this request needs (the host refuses
        the connection when the add-on lacks one).
        """
        is_write = cmd in protocol.WRITE_COMMANDS
        limit = self.deadlines.client_write if is_write else self.deadlines.client_read
        connection = self._connector.open(timeout=5)
        try:
            self._handshake(connection, require)
            started = self._monotonic()
            request_id = self._new_id()
            request = RequestFrame(
                v=PROTOCOL_VERSION,
                id=request_id,
                cmd=cmd.value,
                args=args.model_dump(mode="json", exclude_none=True),
            )
            body = protocol.dump_frame(request)
            outgoing = [body]
            for key, data in (payloads or {}).items():
                for index, piece in enumerate(split_chunks(data)):
                    chunk = ChunkFrame(
                        v=PROTOCOL_VERSION,
                        id=request_id,
                        name=key,
                        index=index,
                        data=base64.b64encode(piece).decode("ascii"),
                        sha256=sha256_bytes(piece),
                    )
                    outgoing.append(protocol.dump_frame(chunk))
            for item in outgoing:  # nothing leaves before every frame fits its limit
                frames.encode_frame(item, frames.TO_ADDON_LIMIT)
            return self._exchange(connection, cmd, request_id, outgoing, started, limit)
        finally:
            connection.close()

    def _exchange(
        self,
        connection: Connection,
        cmd: Command,
        request_id: str,
        outgoing: list[bytes],
        started: float,
        limit: float,
    ) -> Reply:
        write = cmd in protocol.WRITE_COMMANDS
        mutation = "possible" if write else "none"

        def lost(message: str, detail: str = "") -> BridgeFailure:
            return BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE, message, sent=True, mutation=mutation, detail=detail
            )

        try:
            connection.settimeout(limit)
            for item in outgoing:
                frames.write_frame(connection, item, frames.LOCAL_LIMIT)
        except ChamberlainError as error:
            raise lost("The request could not be delivered.", error.detail) from None
        chunks: dict[str, list[ChunkFrame]] = {}
        while True:
            remaining = limit - (self._monotonic() - started)
            if remaining <= 0:
                raise lost("The Thunderbird bridge did not answer in time.")
            try:
                connection.settimeout(remaining)
                raw = frames.read_frame(connection, frames.LOCAL_LIMIT)
            except ChamberlainError as error:
                raise lost("The Thunderbird bridge connection failed.", error.detail) from None
            if raw is None:
                raise lost("The Thunderbird bridge closed the connection.")
            try:
                frame = protocol.parse_frame(raw)
            except ChamberlainError as error:
                raise lost(
                    "The Thunderbird bridge answered in an unknown form.", error.detail
                ) from None
            if isinstance(frame, ChunkFrame) and frame.id == request_id:
                chunks.setdefault(frame.name, []).append(frame)
                continue
            if isinstance(frame, Refusal):
                raise BridgeFailure(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    f"The Thunderbird bridge refused: {frame.code.value}.",
                    sent=False,
                    fix=UPDATE_FIX,
                )
            if isinstance(frame, ResponseFrame) and frame.id == request_id:
                return self._finish(frame, cmd, chunks, mutation)
            raise lost("The Thunderbird bridge sent an unexpected message.")

    def _finish(
        self,
        frame: ResponseFrame,
        cmd: Command,
        chunks: dict[str, list[ChunkFrame]],
        mutation: str,
    ) -> Reply:
        if not frame.ok:
            error = frame.error
            if error is None:
                raise BridgeFailure(
                    ErrorCode.SOURCE_UNAVAILABLE, "Malformed error.", sent=True, mutation=mutation
                )
            raise BridgeFailure(
                _CODE_MAP[error.code],
                _safe(error.code),
                sent=True,
                mutation=error.mutation,
                bridge_code=error.code,
                detail=error.message,
            )
        try:
            result = protocol.parse_result(cmd, frame.result or {})
        except ChamberlainError as error:
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE,
                error.message,
                sent=True,
                mutation=mutation,
                fix=UPDATE_FIX,
                detail=error.detail,
            ) from None
        if sum(len(v) for v in chunks.values()) != frame.chunks:
            raise BridgeFailure(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The Thunderbird bridge lost part of a transfer.",
                sent=True,
                mutation=mutation,
            )
        return Reply(result=result, chunks=chunks)


def _safe(code: BridgeErrorCode) -> str:
    """Harness-written text per code; the add-on's own message goes to `detail` (E-8, E-15)."""
    texts = {
        BridgeErrorCode.BAD_REQUEST: "Thunderbird refused the request as malformed.",
        BridgeErrorCode.UNKNOWN_COMMAND: "The Thunderbird add-on has no such command.",
        BridgeErrorCode.ACCOUNT_MISMATCH: "The Thunderbird account is not the one configured.",
        BridgeErrorCode.NOT_FOUND: "That message or folder is not in Thunderbird.",
        BridgeErrorCode.AMBIGUOUS: "More than one message matches where one is required.",
        BridgeErrorCode.CHANGED: "The message or draft changed since it was read.",
        BridgeErrorCode.BUSY: "Thunderbird is busy with another change.",
        BridgeErrorCode.TIMEOUT: "Thunderbird did not finish in time.",
        BridgeErrorCode.OFFLINE: "Thunderbird is offline.",
        BridgeErrorCode.NOT_VERIFIED: "Thunderbird could not confirm the change.",
        BridgeErrorCode.TOO_LARGE: "That is too large for the Thunderbird bridge.",
        BridgeErrorCode.HASH_MISMATCH: "Attachment bytes did not match their hash.",
        BridgeErrorCode.UNSUPPORTED: "Thunderbird does not support that.",
        BridgeErrorCode.INTERNAL: "The Thunderbird add-on failed.",
    }
    return texts[code]
