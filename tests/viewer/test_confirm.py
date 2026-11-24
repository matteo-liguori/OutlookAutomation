"""The confirm page's contract: exact content, hash-checked confirm, human-only grants.

Rules: docs/contracts/05-grants.md (G-13, G-14, G-15, G-18, G-19).
"""

import json
from typing import Any

from viewer_rig import Rig

from chamberlain.domain.grants import GrantState, StandingScope
from chamberlain.domain.intents import IntentKind
from chamberlain.schema.registry import TOOLS
from chamberlain.writes import repo


def test_g19_the_detail_shows_the_exact_content_to_be_authorised(rig: Rig) -> None:
    proposal = rig.proposal(to="new.person@example.org", subject="Quarterly numbers")
    handle = proposal.grant.handle  # type: ignore[union-attr]
    reply = rig.call("GET", f"/api/grants/{handle}")
    assert reply.status == 200
    detail = reply.body
    assert detail["content_sha256"] == proposal.content_sha256
    for expected in ("new.person@example.org", "Quarterly numbers", "The exact body text."):
        assert expected in detail["rendered"]
    assert detail["state"] == "proposed"
    assert detail["requirement"] == "human_confirmation"


def test_g19_confirm_with_a_different_hash_is_409_and_nothing_is_granted(rig: Rig) -> None:
    proposal = rig.proposal()
    handle = proposal.grant.handle  # type: ignore[union-attr]
    stale = rig.call("POST", f"/api/grants/{handle}/confirm", {"content_sha256": "f" * 64})
    assert stale.status == 409
    assert rig.call("POST", f"/api/grants/{handle}/confirm", {}).status == 409
    assert repo.get_grant(rig.ctx.db, handle).state is GrantState.PROPOSED  # type: ignore[union-attr]
    assert repo.known_recipients(rig.ctx.db, "mail:test") == frozenset()
    kinds = [r.kind for r in repo.audit_rows(rig.ctx.db)]
    assert kinds.count("confirm.rejected") == 2


def test_g19_g15_confirming_the_hash_that_was_shown_activates_and_names_the_human(
    rig: Rig,
) -> None:
    proposal = rig.proposal()
    handle = proposal.grant.handle  # type: ignore[union-attr]
    shown = rig.call("GET", f"/api/grants/{handle}").body["content_sha256"]
    done = rig.call("POST", f"/api/grants/{handle}/confirm", {"content_sha256": shown})
    assert done.status == 200
    assert done.body["state"] == "active"
    assert repo.known_recipients(rig.ctx.db, "mail:test") == {"stranger@example.org"}
    activated = [r for r in repo.audit_rows(rig.ctx.db) if r.kind == "grant.activated"][-1]
    assert activated.actor == "human:viewer"
    row = rig.rows("SELECT confirmed_via FROM grants WHERE handle = ?", handle)[0]
    assert row["confirmed_via"] == "human_viewer"


def test_g19_decline_revoke_and_keep_are_audited_as_the_human(rig: Rig) -> None:
    first = rig.proposal(to="a@example.org").grant.handle  # type: ignore[union-attr]
    assert rig.call("POST", f"/api/grants/{first}/decline", {}).body["state"] == "declined"
    second = rig.engine().propose_standing(
        StandingScope(
            source="mail:test",
            intent_kinds=frozenset({IntentKind.MAIL_SEND}),
            title="Send to a friend",
            recipients=frozenset({"b@example.org"}),
            max_per_day=3,
        )
    )
    handle = second.grant.handle  # type: ignore[union-attr]
    rig.call("POST", f"/api/grants/{handle}/confirm", {"content_sha256": second.content_sha256})
    assert rig.call("POST", f"/api/grants/{handle}/keep", {}).body["state"] == "active"
    assert rig.call("POST", f"/api/grants/{handle}/revoke", {}).body["state"] == "revoked"
    actors = {
        r.actor
        for r in repo.audit_rows(rig.ctx.db)
        if r.kind in ("grant.declined", "grant.kept", "grant.revoked")
    }
    assert actors == {"human:viewer"}


def test_g14_nothing_the_agent_can_call_activates_a_human_only_grant(rig: Rig) -> None:
    """The twelve tools, a forged approval and the CLI's tools all fail to confirm."""
    proposal = rig.proposal(to="not.known@example.org")
    assert proposal.operation is not None
    assert proposal.grant is not None
    grant, operation = proposal.grant.handle, proposal.operation.handle
    started = rig.app.call("start", {"agent": "agent"})
    session = started.data.session  # type: ignore[union-attr]
    evidence = rig.app.call(
        "keep",
        {"session": session, "kind": "evidence", "owner": operation, "reason": "x", "text": "yes"},
    )
    assert evidence.ok, evidence.error
    forged = rig.app.call(
        "propose",
        {
            "session": session,
            "operation": operation,
            "approval_evidence": evidence.data.handle,  # type: ignore[union-attr]
        },
    )
    assert forged.error is not None
    assert forged.error.code.value == "grant.human_confirmation_required"
    assert "token" not in forged.error.fix.lower()
    done = rig.app.call("do", {"session": session, "operation": operation})
    assert done.ok is False
    assert repo.get_grant(rig.ctx.db, grant).state is GrantState.PROPOSED  # type: ignore[union-attr]
    names = {spec.name.value for spec in TOOLS}
    assert not {n for n in names if "confirm" in n or "grant" in n or "viewer" in n}


def test_g18_the_token_is_in_no_tool_reply_no_audit_row_and_no_viewer_reply(rig: Rig) -> None:
    proposal = rig.proposal()
    handle = proposal.grant.handle  # type: ignore[union-attr]
    started = rig.app.call("start", {"agent": "agent"})
    session = started.data.session  # type: ignore[union-attr]
    replies: list[Any] = [started.model_dump(mode="json")]
    for tool, request in (
        ("next", {}),
        ("doctor", {"metrics": True}),
        ("guide", {}),
        ("find", {"kind": "cards"}),
    ):
        replies.append(rig.app.call(tool, {"session": session, **request}).model_dump(mode="json"))
    for path in ("/api/grants", f"/api/grants/{handle}", "/api/overview", "/api/activity"):
        replies.append(rig.call("GET", path).body)
    rig.call("GET", "/api/overview", token="x" + rig.token)
    assert rig.token not in json.dumps(replies)
    dump = " ".join(
        " ".join(str(v) for v in tuple(row))
        for row in rig.ctx.db.execute("SELECT * FROM audit_log")
    )
    assert rig.token not in dump
    assert ("x" + rig.token) not in dump


def test_g18_the_token_file_is_in_run_and_matches_the_launch_token(rig: Rig) -> None:
    saved = (rig.ctx.data_root / "run" / "viewer.token").read_text(encoding="utf-8")
    assert saved == rig.token
