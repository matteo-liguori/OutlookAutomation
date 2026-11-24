"""The request checks and response headers of the viewer (G-17), ported from the old viewer.

The writes track's `ConfirmationApp` checks the token, the Origin of state-changing requests
and the loopback address, and lets a request with no Host header through. The viewer is
stricter and applies one gate to every route: a Host header is required and must be the
viewer's own (this is what stops DNS rebinding), the method must be GET or POST, a browser
must say the request is same-origin (`Sec-Fetch-Site`), and a POST must be JSON.
"""

import hmac
from collections.abc import Mapping
from dataclasses import dataclass

TOKEN_HEADER = "X-Chamberlain-Token"
LOOPBACK = frozenset({"127.0.0.1", "::1"})
SAME_SITE_OK = frozenset({"same-origin", "none"})
ALLOWED_METHODS = frozenset({"GET", "POST"})

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Content-Security-Policy", CSP),
)


@dataclass(frozen=True)
class Refusal:
    """Why a request was refused, for the audit row (never contains the token)."""

    status: int
    why: str


def header(environ: Mapping[str, object], name: str) -> str | None:
    """A request header from a WSGI environ."""
    value = environ.get("HTTP_" + name.upper().replace("-", "_"))
    return value if isinstance(value, str) else None


def check_basics(environ: Mapping[str, object], host: str) -> Refusal | None:
    """Checks every route needs: method, loopback peer, Host, same-origin fetch."""
    method = str(environ.get("REQUEST_METHOD", ""))
    if method not in ALLOWED_METHODS:
        return Refusal(405, "method not allowed")
    if environ.get("REMOTE_ADDR") not in LOOPBACK:
        return Refusal(403, "not from the loopback address")
    if header(environ, "Host") != host:
        return Refusal(403, "unexpected host")
    site = header(environ, "Sec-Fetch-Site")
    if site is not None and site not in SAME_SITE_OK:
        return Refusal(403, "cross-site request")
    return None


def check_api(environ: Mapping[str, object], token: str, origin: str) -> Refusal | None:
    """Checks the data routes add: the token, then Origin and JSON for a POST."""
    supplied = header(environ, TOKEN_HEADER)
    if not supplied:
        return Refusal(403, "token missing")
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        return Refusal(403, "token invalid")
    if environ.get("REQUEST_METHOD") == "POST":
        if header(environ, "Origin") != origin:
            return Refusal(403, "origin mismatch")
        content_type = str(environ.get("CONTENT_TYPE") or "")
        if content_type.partition(";")[0].strip().lower() != "application/json":
            return Refusal(415, "not json")
    return None
