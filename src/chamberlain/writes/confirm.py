"""The confirmation channel: the viewer endpoint's logic, without a server (G-17 to G-19).

Wave 2 mounts `ConfirmationApp` (a WSGI callable) in `chamberlain viewer` on
`127.0.0.1` and creates the token with `create_token`. Every request needs the
token in `X-Chamberlain-Token`; state-changing requests also need an `Origin`
equal to the viewer's own origin. A request failing either check gets 403 and
an audit row. The token is never returned, logged or audited. No tool or CLI
command can activate, keep or edit a grant: this is the only way in.
"""

import contextlib
import hmac
import json
import re
import secrets
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from chamberlain.app.context import Context
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.grant_rules import parse_ts, recipients_of
from chamberlain.domain.grants import ConfirmedVia, GrantKind, GrantState
from chamberlain.schema.errors import ChamberlainError, ErrorCategory, ErrorCode
from chamberlain.writes import repo
from chamberlain.writes.engine import Engine
from chamberlain.writes.models import GrantRow, WritePolicy
from chamberlain.writes.render import describe_intent, describe_scope

TOKEN_HEADER = "X-Chamberlain-Token"
ACTOR = "human:viewer"
REJECT_ACTOR = "system:viewer"
MAX_BODY = 64 * 1024
_HANDLE = r"[a-z]+_[a-z0-9]{8,32}"
_LOOPBACK = frozenset({"127.0.0.1", "::1"})

_GRANT_ACTION = re.compile(rf"^/api/grants/({_HANDLE})/(confirm|decline|revoke|keep)$")
_GRANT_ONE = re.compile(rf"^/api/grants/({_HANDLE})$")
_OP_RESOLVE = re.compile(rf"^/api/operations/({_HANDLE})/resolve$")


@dataclass(frozen=True)
class Response:
    status: int
    body: dict[str, Any]


def create_token(data_root: Path) -> str:
    """A fresh viewer token, kept in memory by the caller and in `run/viewer.token` (G-18).

    The file is written to a sibling and renamed into place, so a reader never sees a
    half-written token, and it is restricted to the user where the platform allows it.
    """
    token = secrets.token_urlsafe(32)
    run = data_root / "run"
    run.mkdir(parents=True, exist_ok=True)
    target = run / "viewer.token"
    partial = run / "viewer.token.partial"
    partial.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):
        partial.chmod(stat.S_IRUSR | stat.S_IWUSR)
    partial.replace(target)
    return token


def _grant_json(grant: GrantRow) -> dict[str, Any]:
    return {
        "handle": grant.handle,
        "kind": grant.kind.value,
        "state": grant.state.value,
        "title": grant.title,
        "requirement": grant.requirement.value,
        "source": grant.source_id,
        "content_sha256": grant.content_sha256,
        "proposed_at": grant.proposed_at.isoformat(),
        "granted_at": None if grant.granted_at is None else grant.granted_at.isoformat(),
        "expires_at": None if grant.expires_at is None else grant.expires_at.isoformat(),
        "review_at": None if grant.review_at is None else grant.review_at.isoformat(),
    }


class ConfirmationApp:
    """Routes the seven endpoints of G-19 and enforces G-17 and G-18."""

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
        self._policy = policy
        self._host = origin.split("://", 1)[-1]

    # --- entry points ---------------------------------------------------------------

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
        *,
        remote: str = "127.0.0.1",
    ) -> Response:
        """Handle one request. `headers` keys are case-insensitive."""
        path, _, query = target.partition("?")
        lowered = {key.lower(): value for key, value in headers.items()}
        ctx = self._context()
        problem = self._check(method, lowered, remote)
        if problem is not None:
            self._reject(ctx, method, path, body, problem)
            return Response(403, {"error": "forbidden", "message": "Request refused."})
        try:
            return self._route(ctx, method, path, query, body)
        except ChamberlainError as error:
            return Response(_status(error), {"error": error.code.value, "message": error.message})

    def __call__(
        self, environ: Mapping[str, Any], start_response: Callable[..., object]
    ) -> Iterable[bytes]:
        """WSGI: `(environ, start_response) -> body chunks`."""
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length > MAX_BODY:
            response = Response(413, {"error": "too_large", "message": "Request too large."})
        else:
            body = environ["wsgi.input"].read(length) if length else b""
            headers = {
                key[5:].replace("_", "-"): value
                for key, value in environ.items()
                if key.startswith("HTTP_") and isinstance(value, str)
            }
            query = environ.get("QUERY_STRING") or ""
            target = environ.get("PATH_INFO", "/") + ("?" + query if query else "")
            response = self.handle(
                environ.get("REQUEST_METHOD", "GET"),
                target,
                headers,
                body,
                remote=environ.get("REMOTE_ADDR", ""),
            )
        payload = json.dumps(response.body).encode("utf-8")
        start_response(
            f"{response.status} {_REASONS.get(response.status, 'Error')}",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(payload))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [payload]

    # --- checks (G-17) --------------------------------------------------------------

    def _check(self, method: str, headers: Mapping[str, str], remote: str) -> str | None:
        if remote not in _LOOPBACK:
            return "not from the loopback address"
        host = headers.get("host")
        if host is not None and host != self._host:
            return "unexpected host"
        supplied = headers.get(TOKEN_HEADER.lower())
        if not supplied:
            return "token missing"
        if not hmac.compare_digest(supplied.encode(), self._token.encode()):
            return "token invalid"
        if method != "GET" and headers.get("origin") != self._origin:
            return "origin mismatch"
        return None

    def _reject(self, ctx: Context, method: str, path: str, body: bytes, why: str) -> None:
        known = (
            _GRANT_ACTION.match(path)
            or _GRANT_ONE.match(path)
            or _OP_RESOLVE.match(path)
            or path == "/api/grants"
        )
        shown = path if known else "(unknown path)"
        ctx.services.audit.append(
            actor=REJECT_ACTOR,
            kind="confirm.rejected",
            subject=None,
            summary=f"403 {method[:8]} {shown}: {why}.",
            payload_sha256=sha256_text(body.decode("utf-8", "replace")) if body else None,
        )

    # --- routing --------------------------------------------------------------------

    def _route(self, ctx: Context, method: str, path: str, query: str, body: bytes) -> Response:
        if method == "GET" and path == "/api/grants":
            states = parse_qs(query).get("state", [])
            return Response(200, {"grants": list_grants(ctx, states)})
        match = _GRANT_ONE.match(path)
        if method == "GET" and match:
            return Response(200, grant_detail(ctx, match.group(1)))
        match = _GRANT_ACTION.match(path)
        if method == "POST" and match:
            handle, action = match.group(1), match.group(2)
            data = _json(body)
            if action == "confirm":
                grant = confirm_grant(ctx, handle, data, self._policy)
            elif action == "decline":
                grant = decline_grant(ctx, handle, self._policy)
            elif action == "revoke":
                grant = revoke_grant(ctx, handle, self._policy)
            else:
                grant = keep_grant(ctx, handle, self._policy)
            return Response(200, _grant_json(grant))
        match = _OP_RESOLVE.match(path)
        if method == "POST" and match:
            return Response(200, resolve_operation(ctx, match.group(1), _json(body), self._policy))
        return Response(404, {"error": "not_found", "message": "No such endpoint."})


_REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
}


def _status(error: ChamberlainError) -> int:
    if error.code is ErrorCode.REQUEST_UNKNOWN_HANDLE:
        return 404
    if error.category in (ErrorCategory.CONFLICT, ErrorCategory.NOT_AUTHORISED):
        return 409
    return 400


def _json(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data: object = json.loads(body)
    except ValueError:
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "The body is not JSON.") from None
    if not isinstance(data, dict):
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "The body is not an object.")
    return {str(key): value for key, value in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


# --- the seven operations (plain functions; tests call them directly) -------------


def list_grants(ctx: Context, states: Iterable[str] = ()) -> list[dict[str, Any]]:
    """`GET /api/grants?state=` (reading never renews, G-7)."""
    try:
        wanted = [GrantState(state) for state in states] or list(GrantState)
    except ValueError:
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "Unknown state.", field="state") from None
    return [_grant_json(grant) for grant in repo.grants_in_states(ctx.db, wanted)]


def grant_detail(ctx: Context, handle: str) -> dict[str, Any]:
    """`GET /api/grants/{handle}`: exactly what would be authorised, with its hash."""
    grant = _grant(ctx, handle)
    detail = _grant_json(grant)
    if grant.scope is not None:
        detail["scope"] = json.loads(grant.scope.model_dump_json(exclude_none=True))
        detail["rendered"] = describe_scope(grant.scope)
        return detail
    ops = repo.operations_by_hash(ctx.db, grant.source_id, grant.content_sha256)
    shown = next((op for op in reversed(ops) if op.intent_json is not None), None)
    if shown is not None:
        intent = Engine.intent_of(shown)
        detail["operation"] = shown.handle
        detail["intent"] = json.loads(shown.intent_json or "{}")
        detail["rendered"] = describe_intent(
            intent, limit=None, refs=json.loads(shown.refs_json or "{}")
        )[0]
    return detail


def _grant(ctx: Context, handle: str) -> GrantRow:
    grant = repo.get_grant(ctx.db, handle)
    if grant is None:
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such grant.")
    return grant


def _reject_mismatch(ctx: Context, handle: str, summary: str) -> None:
    ctx.services.audit.append(actor=ACTOR, kind="confirm.rejected", subject=handle, summary=summary)


def confirm_grant(
    ctx: Context, handle: str, data: Mapping[str, Any], policy: WritePolicy | None = None
) -> GrantRow:
    """`POST .../confirm`: activates only if the person saw exactly what is granted (G-19)."""
    grant = _grant(ctx, handle)
    shown = data.get("content_sha256")
    if not isinstance(shown, str) or not hmac.compare_digest(shown, grant.content_sha256):
        _reject_mismatch(ctx, handle, "Confirmation refused: the content hash differs.")
        raise ChamberlainError(
            ErrorCode.GRANT_CONTENT_MISMATCH, "What you saw is not what is stored; reopen it."
        )
    expires: datetime | None = None
    requested = data.get("expires_at")
    if isinstance(requested, str):
        try:
            expires = parse_ts(requested)
        except ValueError:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "Bad expiry.", field="expires_at"
            ) from None
    engine = Engine(ctx, policy)
    recipients = _named_recipients(ctx, grant)
    return engine.grants.activate(
        handle,
        via=ConfirmedVia.HUMAN_VIEWER,
        actor=ACTOR,
        requested_expiry=expires,
        recipients=recipients,
    )


def _named_recipients(ctx: Context, grant: GrantRow) -> frozenset[str]:
    """Addresses a human-confirmed grant names; they become known (G-15)."""
    if grant.scope is not None:
        return frozenset(grant.scope.recipients)
    if grant.kind is not GrantKind.ONE_OFF:
        return frozenset()
    for op in repo.operations_by_hash(ctx.db, grant.source_id, grant.content_sha256):
        if op.intent_json is not None:
            return recipients_of(Engine.intent_of(op))
    return frozenset()


def decline_grant(ctx: Context, handle: str, policy: WritePolicy | None = None) -> GrantRow:
    """`POST .../decline`: `proposed -> declined`."""
    return Engine(ctx, policy).grants.decline(handle, actor=ACTOR)


def revoke_grant(ctx: Context, handle: str, policy: WritePolicy | None = None) -> GrantRow:
    """`POST .../revoke`: `active | suspended -> revoked`."""
    grant = _grant(ctx, handle)
    if grant.state not in (GrantState.ACTIVE, GrantState.SUSPENDED):
        raise ChamberlainError(ErrorCode.GRANT_NOT_ACTIVE, "Only a live grant can be revoked.")
    return Engine(ctx, policy).grants.revoke(handle, actor=ACTOR)


def keep_grant(ctx: Context, handle: str, policy: WritePolicy | None = None) -> GrantRow:
    """`POST .../keep`: review done; advances `review_at` and nothing else (G-7)."""
    return Engine(ctx, policy).grants.keep(handle, actor=ACTOR)


def resolve_operation(
    ctx: Context, handle: str, data: Mapping[str, Any], policy: WritePolicy | None = None
) -> dict[str, Any]:
    """`POST /api/operations/{handle}/resolve` for an uncertain operation (W-13)."""
    outcome = data.get("outcome")
    if outcome not in ("happened", "did_not_happen"):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "Outcome is happened or did_not_happen.", field="outcome"
        )
    engine = Engine(ctx, policy)
    op = engine.resolve(handle, happened=outcome == "happened", actor=ACTOR)
    return {"handle": op.handle, "state": op.state.value}
