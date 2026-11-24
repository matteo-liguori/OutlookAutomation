"""Length-prefixed frames with explicit size limits.

Contract: docs/contracts/12-bridge-v2.md section 2. A frame is a 4-byte
unsigned little-endian length followed by that many bytes of UTF-8 JSON.
Zero length, a length over the direction's limit, a body cut short and a
header cut short are all refused.
"""

import struct
from typing import Final, Protocol

from chamberlain.schema.errors import ChamberlainError, ErrorCode

TO_ADDON_LIMIT: Final = 900_000
"""Host to Thunderbird: Mozilla caps this direction at 1 MiB."""
FROM_ADDON_LIMIT: Final = 64 * 1024 * 1024
"""Thunderbird to host."""
LOCAL_LIMIT: Final = 64 * 1024 * 1024
"""Between the host and a local client."""
CHUNK_BYTES: Final = 192 * 1024
"""Raw bytes per attachment chunk; base64 of it stays far below `TO_ADDON_LIMIT`."""

_HEADER = struct.Struct("<I")


class Readable(Protocol):
    def read(self, size: int, /) -> bytes: ...


class Writable(Protocol):
    def write(self, data: bytes | memoryview, /) -> int | None: ...

    def flush(self) -> None: ...


def _unavailable(message: str, detail: str) -> ChamberlainError:
    return ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, message, detail=detail)


def _read_exact(stream: Readable, size: int) -> bytes | None:
    """`size` bytes, or None if the stream ended before the first byte; short means truncated."""
    chunks = bytearray()
    while len(chunks) < size:
        try:
            part = stream.read(size - len(chunks))
        except OSError as error:
            raise _unavailable("The Thunderbird bridge connection failed.", str(error)) from None
        if not part:
            if not chunks:
                return None
            raise _unavailable(
                "The Thunderbird bridge sent a truncated message.",
                f"got {len(chunks)} of {size} bytes",
            )
        chunks.extend(part)
    return bytes(chunks)


def read_frame(stream: Readable, limit: int) -> bytes | None:
    """The next frame body, or None at a clean end of stream."""
    header = _read_exact(stream, _HEADER.size)
    if header is None:
        return None
    (size,) = _HEADER.unpack(header)
    if size == 0 or size > limit:
        raise _unavailable(
            "The Thunderbird bridge sent a message of an invalid size.", f"length {size}"
        )
    body = _read_exact(stream, size)
    if body is None:
        raise _unavailable("The Thunderbird bridge sent a truncated message.", "no body")
    return body


def encode_frame(body: bytes, limit: int) -> bytes:
    if not body or len(body) > limit:
        raise ChamberlainError(
            ErrorCode.REQUEST_TOO_LARGE,
            "That message is too large for the Thunderbird bridge.",
            detail=f"{len(body)} bytes over limit {limit}",
        )
    return _HEADER.pack(len(body)) + body


def write_frame(stream: Writable, body: bytes, limit: int) -> None:
    pending = memoryview(encode_frame(body, limit))
    try:
        while pending:
            written = stream.write(pending)
            if not written:
                raise OSError("frame write made no progress")
            pending = pending[written:]
        stream.flush()
    except (OSError, ValueError) as error:
        raise _unavailable("The Thunderbird bridge connection failed.", str(error)) from None
