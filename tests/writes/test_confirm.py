"""The confirmation channel (05-grants.md G-14, G-17 to G-19)."""

import io
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from chamberlain.connectors.fake import Fault
from chamberlain.domain.grants import GrantState
from chamberlain.domain.operations import OperationState
from chamberlain.testing.write_rig import MAIL, SESSION, WriteRig
from chamberlain.writes import repo
from chamberlain.writes.audit import verify
from chamberlain.writes.confirm import ConfirmationApp, Response, create_token

ORIGIN = "http://127.0.0.1:5555"
TOKEN_HEADER = "X-Chamberlain-Token"


class Viewer:
    def __init__(self, rig: WriteRig, token: str) -> None:
        self.rig = rig
        self.token = token
        self.app = ConfirmationApp(
            lambda: rig.engine().ctx, token=token, origin=ORIGIN, policy=rig.policy
        )

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = "",
        origin: str | None = ORIGIN,
        remote: str = "127.0.0.1",
        host: str = "127.0.0.1:5555",
    ) -> Response:
        headers: dict[str, str] = {"Host": host}
        if token is not None:
            headers[TOKEN_HEADER] = self.token if token == "" else token
        if origin is not None:
            headers["Origin"] = origin
        raw = json.dumps(body).encode() if body is not None else b""
        return self.app.handle(method, path, headers, raw, remote=remote)


@pytest.fixture
def viewer(rig: WriteRig, tmp_path: Path) -> Viewer:
    return Viewer(rig, create_token(tmp_path))


def rejected_rows(rig: WriteRig) -> list[Any]:
    return [r for r in repo.audit_rows(rig.db) if r.kind == "confirm.rejected"]


def new_proposal(rig: WriteRig, to: str = "stranger@example.org", **kwargs: str):  # type: ignore[no-untyped-def]
    proposal = rig.engine().propose_intent(rig.send(to=(to,), **kwargs))
    assert proposal.grant is not None
    return proposal


def test_g17_a_request_without_the_token_is_403_and_audited(rig: WriteRig, viewer: Viewer) -> None:
    proposal = new_proposal(rig)
    path = f"/api/grants/{proposal.grant.handle}/confirm"  # type: ignore[union-attr]
    reply = viewer.call("POST", path, {"content_sha256": proposal.content_sha256}, token=None)
    assert reply.status == 403
    assert repo.get_grant(rig.db, proposal.grant.handle).state is GrantState.PROPOSED  # type: ignore[union-attr]
    rows = rejected_rows(rig)
    assert len(rows) == 1
    assert "token missing" in rows[0].summary
    assert rows[0].actor == "system:viewer"


def test_g17_a_wrong_token_origin_host_or_address_is_403_and_nothing_changes(
    rig: WriteRig, viewer: Viewer
) -> None:
    proposal = new_proposal(rig)
    path = f"/api/grants/{proposal.grant.handle}/confirm"  # type: ignore[union-attr]
    body = {"content_sha256": proposal.content_sha256}
    cases = [
        viewer.call("POST", path, body, token="wrong-token"),
        viewer.call("POST", path, body, origin="http://evil.example"),
        viewer.call("POST", path, body, origin=None),
        viewer.call("POST", path, body, host="evil.example"),
        viewer.call("POST", path, body, remote="10.1.2.3"),
        viewer.call("GET", "/api/grants", token="wrong-token"),
    ]
    assert [c.status for c in cases] == [403] * 6
    assert len(rejected_rows(rig)) == 6
    assert repo.get_grant(rig.db, proposal.grant.handle).state is GrantState.PROPOSED  # type: ignore[union-attr]


def test_g17_reads_need_the_token_but_not_an_origin(rig: WriteRig, viewer: Viewer) -> None:
    new_proposal(rig)
    reply = viewer.call("GET", "/api/grants?state=proposed", origin=None)
    assert reply.status == 200
    assert len(reply.body["grants"]) == 1
    assert viewer.call("GET", "/api/grants?state=nonsense").status == 400


def test_g19_confirm_with_a_different_hash_is_409_and_activates_nothing(
    rig: WriteRig, viewer: Viewer
) -> None:
    proposal = new_proposal(rig)
    handle = proposal.grant.handle  # type: ignore[union-attr]
    reply = viewer.call("POST", f"/api/grants/{handle}/confirm", {"content_sha256": "f" * 64})
    assert reply.status == 409
    assert repo.get_grant(rig.db, handle).state is GrantState.PROPOSED  # type: ignore[union-attr]
    assert repo.known_recipients(rig.db, MAIL) == frozenset()
    assert len(rejected_rows(rig)) == 1
    missing = viewer.call("POST", f"/api/grants/{handle}/confirm", {})
    assert missing.status == 409


def test_g19_g15_confirming_the_exact_hash_activates_and_makes_recipients_known(
    rig: WriteRig, viewer: Viewer
) -> None:
    proposal = new_proposal(rig)
    handle = proposal.grant.handle  # type: ignore[union-attr]
    reply = viewer.call(
        "POST", f"/api/grants/{handle}/confirm", {"content_sha256": proposal.content_sha256}
    )
    assert reply.status == 200
    assert reply.body["state"] == "active"
    assert repo.known_recipients(rig.db, MAIL) == {"stranger@example.org"}
    assert proposal.operation is not None
    assert rig.engine().do(proposal.operation.handle).state is OperationState.CONFIRMED
    last = [r for r in repo.audit_rows(rig.db) if r.kind == "grant.activated"][-1]
    assert last.actor == "human:viewer"


def test_g19_the_detail_shows_exactly_what_would_be_authorised(
    rig: WriteRig, viewer: Viewer
) -> None:
    proposal = new_proposal(rig, subject="Quarterly numbers", body="The full body text.")
    handle = proposal.grant.handle  # type: ignore[union-attr]
    detail = viewer.call("GET", f"/api/grants/{handle}").body
    assert detail["content_sha256"] == proposal.content_sha256
    assert detail["intent"]["to"] == ["stranger@example.org"]
    assert detail["intent"]["subject"] == "Quarterly numbers"
    assert "The full body text." in detail["rendered"]
    standing = rig.standing_grant()
    shown = viewer.call("GET", f"/api/grants/{standing.handle}").body
    assert shown["scope"]["recipients"] == ["friend@example.org"]
    assert shown["content_sha256"] == standing.content_sha256
    assert "At most 5 a day" in shown["rendered"]


def test_g19_decline_revoke_and_keep(rig: WriteRig, viewer: Viewer) -> None:
    declined = new_proposal(rig, to="no@example.org")
    reply = viewer.call("POST", f"/api/grants/{declined.grant.handle}/decline")  # type: ignore[union-attr]
    assert reply.body["state"] == "declined"
    standing = rig.standing_grant()
    assert viewer.call("POST", f"/api/grants/{standing.handle}/keep").body["state"] == "active"
    assert viewer.call("POST", f"/api/grants/{standing.handle}/revoke").body["state"] == "revoked"
    assert viewer.call("POST", f"/api/grants/{standing.handle}/revoke").status == 409
    actors = {
        r.actor
        for r in repo.audit_rows(rig.db)
        if r.kind in ("grant.declined", "grant.revoked", "grant.kept")
    }
    assert actors == {"human:viewer"}


def test_w41_confirmation_app_cannot_settle_an_uncertain_operation(
    rig: WriteRig, viewer: Viewer
) -> None:
    op = rig.ready_send()
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(Exception):  # noqa: B017, PT011 - the uncertain error
        rig.engine().do(op.handle)
    reply = viewer.call(
        "POST", f"/api/operations/{op.handle}/resolve", {"outcome": "did_not_happen"}
    )
    assert reply.status == 404
    assert rig.engine().get(op.handle).state is OperationState.UNCERTAIN


def test_g18_the_token_never_appears_in_a_reply_a_log_line_or_an_audit_row(
    rig: WriteRig, viewer: Viewer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    proposal = new_proposal(rig)
    handle = proposal.grant.handle  # type: ignore[union-attr]
    replies = [
        viewer.call("GET", "/api/grants"),
        viewer.call("GET", f"/api/grants/{handle}"),
        viewer.call("POST", f"/api/grants/{handle}/confirm", {"content_sha256": "0" * 64}),
        viewer.call(
            "POST", f"/api/grants/{handle}/confirm", {"content_sha256": proposal.content_sha256}
        ),
        viewer.call("GET", "/api/grants", token="x" + viewer.token),
        viewer.call("POST", f"/api/grants/{handle}/keep", token=None),
    ]
    assert viewer.token not in json.dumps([r.body for r in replies])
    assert viewer.token not in caplog.text
    wrong = "x" + viewer.token
    dump = " ".join(
        " ".join(str(v) for v in tuple(row)) for row in rig.db.execute("SELECT * FROM audit_log")
    )
    assert viewer.token not in dump
    assert wrong not in dump
    assert viewer.token not in repr(viewer.app.__dict__.get("_context"))


def test_g18_the_token_file_is_kept_under_run_and_is_fresh_each_time(tmp_path: Path) -> None:
    first = create_token(tmp_path)
    assert (tmp_path / "run" / "viewer.token").read_text(encoding="utf-8") == first
    assert create_token(tmp_path) != first
    assert len(first) >= 40


def test_g18_a_token_is_never_accepted_as_a_tool_argument() -> None:
    from chamberlain.schema.errors import ChamberlainError
    from chamberlain.schema.registry import get_tool, parse_request

    spec = get_tool("propose")
    with pytest.raises(ChamberlainError) as raised:
        parse_request(
            spec, {"session": SESSION, "operation": "op_0123456789abcdef", "token": "abc"}
        )
    assert raised.value.code.value == "request.invalid"


def test_g17_the_wsgi_face_enforces_the_same_checks_and_returns_json(
    rig: WriteRig, viewer: Viewer
) -> None:
    new_proposal(rig)
    seen: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        seen["status"] = status
        seen["headers"] = dict(headers)

    def request(token: str | None) -> bytes:
        environ: dict[str, Any] = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/api/grants",
            "QUERY_STRING": "state=proposed",
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "127.0.0.1:5555",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
        }
        if token is not None:
            environ["HTTP_X_CHAMBERLAIN_TOKEN"] = token
        return b"".join(viewer.app(environ, start_response))

    assert request(None)
    assert seen["status"].startswith("403")
    body = json.loads(request(viewer.token))
    assert seen["status"].startswith("200")
    assert seen["headers"]["Content-Type"] == "application/json"
    assert len(body["grants"]) == 1


def test_g20_the_whole_exchange_leaves_a_verifying_chain(rig: WriteRig, viewer: Viewer) -> None:
    proposal = new_proposal(rig)
    handle = proposal.grant.handle  # type: ignore[union-attr]
    viewer.call("POST", f"/api/grants/{handle}/confirm", {"content_sha256": "1" * 64}, token=None)
    viewer.call(
        "POST", f"/api/grants/{handle}/confirm", {"content_sha256": proposal.content_sha256}
    )
    assert verify(rig.db).ok
    assert SESSION  # the agent session is unrelated to the viewer's actor


def _unused(_: Callable[..., None]) -> None:
    return None
