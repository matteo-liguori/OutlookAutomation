"""The viewer's request checks: token, Origin, Host, peer, method, content type (G-17).

Rules: docs/contracts/05-grants.md (G-17, G-18).
"""

from typing import Any

import pytest
from viewer_rig import Rig

from chamberlain.domain.grants import GrantState
from chamberlain.writes import repo


def rejected(rig: Rig) -> list[Any]:
    return [r for r in repo.audit_rows(rig.ctx.db) if r.kind == "confirm.rejected"]


def test_g17_the_shell_needs_no_token_but_holds_no_records(rig: Rig) -> None:
    rig.proposal(subject="A private subject")
    for path in (
        "/",
        "/index.html",
        "/static/page.js",
        "/static/page.css",
        "/static/calendar.js",
        "/static/calendar.css",
    ):
        reply = rig.call("GET", path, token=None, origin=None)
        assert reply.status == 200, path
    shell = rig.last_bytes.decode()
    assert "private" not in shell.lower()
    assert "stranger@example.org" not in shell
    assert rig.call("GET", "/static/../../pyproject.toml", token=None).status == 404
    assert rig.call("GET", "/static/other.js", token=None).status == 404
    assert rig.rows("SELECT * FROM audit_log WHERE kind = 'confirm.rejected'") == []


def test_g17_every_response_carries_the_hardening_headers(rig: Rig) -> None:
    for path in ("/", "/api/overview", "/api/grants", "/nope"):
        rig.call("GET", path)
        headers = rig.last_headers
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Frame-Options"] == "DENY"
        csp = headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "http" not in csp.replace("'self'", "")


def test_g17_the_shell_loads_nothing_from_the_internet(rig: Rig) -> None:
    rig.call("GET", "/", token=None)
    html = rig.last_bytes.decode()
    rig.call("GET", "/static/page.js", token=None)
    script = rig.last_bytes.decode()
    rig.call("GET", "/static/page.css", token=None)
    css = rig.last_bytes.decode()
    rig.call("GET", "/static/calendar.js", token=None)
    calendar_script = rig.last_bytes.decode()
    rig.call("GET", "/static/calendar.css", token=None)
    calendar_css = rig.last_bytes.decode()
    for text in (html, script, css, calendar_script, calendar_css):
        assert "https://" not in text.replace("[https:", "").replace('"https:"', "")
        assert "//cdn" not in text
        assert "@import" not in text
        assert "url(" not in text
    for code in (script, calendar_script):
        assert "innerHTML" not in code  # nothing from a mail can become markup
        assert "outerHTML" not in code
        assert "insertAdjacentHTML" not in code
        assert "eval(" not in code


def test_g17_a_missing_or_wrong_token_is_403_audited_and_changes_nothing(rig: Rig) -> None:
    proposal = rig.proposal()
    handle = proposal.grant.handle  # type: ignore[union-attr]
    body = {"content_sha256": proposal.content_sha256}
    path = f"/api/grants/{handle}/confirm"
    assert rig.call("POST", path, body, token=None).status == 403
    assert rig.call("POST", path, body, token="wrong").status == 403
    assert rig.call("GET", "/api/overview", token=None).status == 403
    assert rig.call("GET", "/api/overview", token="wrong").status == 403
    assert repo.get_grant(rig.ctx.db, handle).state is GrantState.PROPOSED  # type: ignore[union-attr]
    rows = rejected(rig)
    assert len(rows) == 4
    assert {r.actor for r in rows} == {"system:viewer"}
    assert any("token missing" in r.summary for r in rows)
    assert all(rig.token not in r.summary for r in rows)


def test_g17_a_wrong_origin_host_peer_or_fetch_site_is_403(rig: Rig) -> None:
    proposal = rig.proposal()
    handle = proposal.grant.handle  # type: ignore[union-attr]
    path = f"/api/grants/{handle}/confirm"
    body = {"content_sha256": proposal.content_sha256}
    cases = [
        rig.call("POST", path, body, origin="http://evil.example"),
        rig.call("POST", path, body, origin=None),
        rig.call("POST", path, body, host="evil.example"),
        rig.call("POST", path, body, host=None),
        rig.call("POST", path, body, remote="10.1.2.3"),
        rig.call("POST", path, body, headers={"Sec-Fetch-Site": "cross-site"}),
        rig.call("GET", "/api/overview", host="localhost:5555"),
        rig.call("GET", "/api/overview", host=None),
        rig.call("GET", "/", token=None, host="evil.example"),
        rig.call("GET", "/", token=None, host=None),
    ]
    assert [c.status for c in cases] == [403] * 10
    assert repo.get_grant(rig.ctx.db, handle).state is GrantState.PROPOSED  # type: ignore[union-attr]


def test_g17_a_post_must_be_json_and_the_method_must_be_get_or_post(rig: Rig) -> None:
    handle = rig.proposal().grant.handle  # type: ignore[union-attr]
    assert (
        rig.call("POST", f"/api/grants/{handle}/keep", {}, content_type="text/plain").status == 415
    )
    assert rig.call("POST", f"/api/grants/{handle}/keep", {}, content_type=None).status == 415
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
        assert rig.call(method, "/api/overview").status == 405
    assert rig.call("GET", "/api/overview").status == 200


def test_g17_a_loopback_ipv6_peer_is_allowed(rig: Rig) -> None:
    assert rig.call("GET", "/api/overview", remote="::1").status == 200


def test_g17_an_unknown_path_is_404_after_the_token_check(rig: Rig) -> None:
    assert rig.call("GET", "/api/nothing").status == 404
    assert rig.call("GET", "/api/nothing", token=None).status == 403
    assert rig.call("GET", "/elsewhere", token=None).status == 404


def test_g18_an_audited_refusal_never_records_a_hostile_path(rig: Rig) -> None:
    rig.call("GET", "/api/<script>alert(1)</script>", token=None)
    rows = rejected(rig)
    assert len(rows) == 1
    assert "<script>" not in rows[0].summary
    assert "(path)" in rows[0].summary


@pytest.mark.parametrize("path", ["/api/grants", "/api/operations/op_0123456789abcdef/resolve"])
def test_g17_the_mounted_confirmation_routes_sit_behind_the_same_gate(rig: Rig, path: str) -> None:
    assert rig.call("GET", path, token=None).status == 403
    assert rig.call("GET", path, host="evil.example").status == 403
