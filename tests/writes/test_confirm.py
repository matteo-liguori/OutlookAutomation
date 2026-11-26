"""The confirmation channel (05-grants.md G-14, G-17 to G-19)."""

import json
from pathlib import Path
from typing import Any

import pytest

from chamberlain.connectors.fake import Fault
from chamberlain.domain.operations import OperationState
from chamberlain.writes.audit import verify
from chamberlain.writes.confirm import ConfirmationApp, Response, create_token
from support.write_rig import SESSION, WriteRig

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


def new_proposal(rig: WriteRig, to: str = "stranger@example.org", **kwargs: str):  # type: ignore[no-untyped-def]
    proposal = rig.engine().propose_intent(rig.send(to=(to,), **kwargs))
    assert proposal.grant is not None
    return proposal


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


def test_g20_the_whole_exchange_leaves_a_verifying_chain(rig: WriteRig, viewer: Viewer) -> None:
    proposal = new_proposal(rig)
    handle = proposal.grant.handle  # type: ignore[union-attr]
    viewer.call("POST", f"/api/grants/{handle}/confirm", {"content_sha256": "1" * 64}, token=None)
    viewer.call(
        "POST", f"/api/grants/{handle}/confirm", {"content_sha256": proposal.content_sha256}
    )
    assert verify(rig.db).ok
    assert SESSION  # the agent session is unrelated to the viewer's actor
