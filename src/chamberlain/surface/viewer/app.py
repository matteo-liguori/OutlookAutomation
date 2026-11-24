"""The viewer's WSGI application: one gate in front of the shell, the views and the confirmations.

Contract: docs/contracts/05-grants.md (G-14, G-17 to G-19). Routes:

- `GET /` and `GET /static/page.js|page.css`: the shell. They hold no records, so they need no
  token, but they still need a loopback peer and the viewer's own Host (a rebinding page gets 403).
- `/api/grants...` and `/api/operations/...`: handed to the writes track's `ConfirmationApp`
  (list, exact content, confirm with the content hash, decline, revoke, keep, resolve).
- every other `/api/...` route is a read-only view, plus the person's notes.

Every API request carries the token in `X-Chamberlain-Token`; a POST also needs the viewer's own
Origin and a JSON body. A failure is a 403 (or 405 or 415) and, for the API, an audit row.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from chamberlain.app.context import Context
from chamberlain.domain.canonical import sha256_text
from chamberlain.runs.notes import add_note, withdraw_note
from chamberlain.schema.errors import ChamberlainError, ErrorCategory, ErrorCode
from chamberlain.surface.viewer import views
from chamberlain.surface.viewer.gate import SECURITY_HEADERS, Refusal, check_api, check_basics
from chamberlain.writes.confirm import MAX_BODY, ConfirmationApp
from chamberlain.writes.models import WritePolicy

REJECT_ACTOR = "system:viewer"
STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/page.js": ("page.js", "text/javascript; charset=utf-8"),
    "/static/page.css": ("page.css", "text/css; charset=utf-8"),
}
_DELEGATED = ("/api/grants", "/api/operations/")
_SAFE_PATH = re.compile(r"^/api/[A-Za-z0-9_/.-]{0,70}$")
_RUN_REPORT = re.compile(r"^/api/runs/(run_[a-z0-9]{8,32})/reports$")
_NOTE_WITHDRAW = re.compile(r"^/api/notes/(\d{1,9})/withdraw$")
_REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
}

StartResponse = Callable[..., object]


class ViewerApp:
    """Implements WSGI. One instance serves one launch (one token, one origin)."""

    def __init__(
        self,
        context: Callable[[], Context],
        *,
        token: str,
        origin: str,
        policy: WritePolicy | None = None,
    ) -> None:
        self._context = context
        self._token = token
        self._origin = origin
        self._host = origin.split("://", 1)[-1]
        self._zone = "UTC" if policy is None else policy.zone
        self._confirm = ConfirmationApp(context, token=token, origin=origin, policy=policy)

    # --- WSGI -------------------------------------------------------------------------

    def __call__(
        self, environ: Mapping[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        path = str(environ.get("PATH_INFO", "/"))
        refusal = check_basics(environ, self._host)
        if refusal is None and path.startswith("/api/"):
            refusal = check_api(environ, self._token, self._origin)
        if refusal is not None:
            if path.startswith("/api/"):
                self._audit_refusal(environ, path, refusal)
            return self._reply(start_response, refusal.status, _error("forbidden", "Refused."))
        if path in STATIC_FILES:
            return self._static(environ, start_response, path)
        if path.startswith(_DELEGATED):
            return self._confirm(environ, _with_headers(start_response))
        if path.startswith("/api/"):
            return self._api(environ, start_response, path)
        return self._reply(start_response, 404, _error("not_found", "No such page."))

    # --- pieces -----------------------------------------------------------------------

    def _audit_refusal(self, environ: Mapping[str, Any], path: str, refusal: Refusal) -> None:
        """One audit row per refused API request. The token is never recorded (G-18)."""
        shown = path if _SAFE_PATH.match(path) else "(path)"
        method = str(environ.get("REQUEST_METHOD", ""))[:8]
        try:
            self._context().services.audit.append(
                actor=REJECT_ACTOR,
                kind="confirm.rejected",
                subject=None,
                summary=f"{refusal.status} {method} {shown}: {refusal.why}.",
                payload_sha256=sha256_text(path),
            )
        except Exception:  # an audit failure must not turn a refusal into an error page
            return

    def _static(
        self, environ: Mapping[str, Any], start_response: StartResponse, path: str
    ) -> Iterable[bytes]:
        if environ.get("REQUEST_METHOD") != "GET":
            return self._reply(start_response, 405, _error("method", "Use GET."))
        name, content_type = STATIC_FILES[path]
        body = (STATIC_DIR / name).read_bytes()
        start_response(
            "200 OK",
            [("Content-Type", content_type), ("Content-Length", str(len(body))), *SECURITY_HEADERS],
        )
        return [body]

    def _api(
        self, environ: Mapping[str, Any], start_response: StartResponse, path: str
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD"))
        try:
            body = self._body(environ) if method == "POST" else b""
            status, data = self._route(self._context(), method, path, environ, body)
        except ChamberlainError as error:
            status, data = _status(error), _error(error.code.value, error.message)
        return self._reply(start_response, status, data)

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> bytes:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length > MAX_BODY:
            raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "Request too large.")
        return environ["wsgi.input"].read(length) if length else b""

    def _route(
        self, ctx: Context, method: str, path: str, environ: Mapping[str, Any], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        if method == "GET":
            return 200, self._read(ctx, path, query)
        if path == "/api/notes":
            text = _json(body).get("text")
            if not isinstance(text, str):
                raise ChamberlainError(ErrorCode.REQUEST_INVALID, "Give the note text.")
            return 200, {"id": add_note(ctx, text)}
        match = _NOTE_WITHDRAW.match(path)
        if match:
            withdraw_note(ctx, int(match.group(1)))
            return 200, {"withdrawn": int(match.group(1))}
        return 404, _error("not_found", "No such endpoint.")

    def _read(self, ctx: Context, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        if path == "/api/overview":
            return views.overview(ctx)
        if path == "/api/work":
            return views.work(ctx)
        if path == "/api/uncertain":
            return views.uncertain(ctx)
        if path == "/api/activity":
            return views.activity(ctx, self._zone, (query.get("day") or [None])[0])
        if path == "/api/runs":
            return views.runs(ctx)
        if path == "/api/storage":
            return views.storage(ctx)
        if path == "/api/notes":
            return views.notes(ctx)
        match = _RUN_REPORT.match(path)
        if match:
            return views.run_reports(ctx, match.group(1))
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such endpoint.")

    @staticmethod
    def _reply(start_response: StartResponse, status: int, data: dict[str, Any]) -> list[bytes]:
        payload = json.dumps(data).encode("utf-8")
        start_response(
            f"{status} {_REASONS.get(status, 'Error')}",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(payload))),
                *SECURITY_HEADERS,
            ],
        )
        return [payload]


def _with_headers(start_response: StartResponse) -> StartResponse:
    """Add the security headers to what the confirmation app answers."""

    def wrapped(status: str, headers: list[tuple[str, str]], *rest: Any) -> object:
        present = {name.lower() for name, _ in headers}
        extra = [(n, v) for n, v in SECURITY_HEADERS if n.lower() not in present]
        return start_response(status, [*headers, *extra], *rest)

    return wrapped


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": code, "message": message}


def _status(error: ChamberlainError) -> int:
    if error.code is ErrorCode.REQUEST_UNKNOWN_HANDLE:
        return 404
    if error.code is ErrorCode.REQUEST_TOO_LARGE:
        return 413
    if error.category in (ErrorCategory.CONFLICT, ErrorCategory.NOT_AUTHORISED):
        return 409
    return 400


def _json(body: bytes) -> dict[str, Any]:
    try:
        data: object = json.loads(body or b"{}")
    except ValueError:
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "The body is not JSON.") from None
    if not isinstance(data, dict):
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "The body is not an object.")
    return {str(key): value for key, value in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
