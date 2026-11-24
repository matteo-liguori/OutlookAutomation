"""A small HTTP transport that says whether a request may have reached the server.

Contract: docs/contracts/08-ports.md (P-10, P-17). `TransportFailure.sent` is
False only when no byte of the request can have been delivered (connection
refused, name not found); a timeout or reset after connecting is `sent=True`.
"""

import socket
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from chamberlain.schema.errors import ChamberlainError, ErrorCode

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


@dataclass(frozen=True)
class HttpReply:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    """Header names lower-cased."""
    body: bytes = b""


class TransportFailure(Exception):
    """No answer was obtained. `sent` says whether the request may have arrived."""

    def __init__(self, message: str, *, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


class HttpTransport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpReply: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


class UrllibTransport:
    """The real transport. Never follows redirects and never retries."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpReply:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return _reply(response.status, response.headers.items(), response.read())
        except urllib.error.HTTPError as error:
            return _reply(error.code, error.headers.items(), error.read())
        except urllib.error.URLError as error:
            refused = isinstance(error.reason, ConnectionRefusedError | socket.gaierror)
            raise TransportFailure(type(error.reason).__name__, sent=not refused) from None
        except (TimeoutError, OSError) as error:
            raise TransportFailure(type(error).__name__, sent=True) from None


def _reply(status: int, items: Iterable[tuple[str, str]], body: bytes) -> HttpReply:
    return HttpReply(status, {k.lower(): v for k, v in items}, body)


def require_secure(url: str) -> str:
    """Refuse plain http except to this machine, so a password never crosses a network in clear."""
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS):
        return url
    raise ChamberlainError(
        ErrorCode.CONFIG_INVALID,
        "The calendar address must use https.",
        field="url",
    )
