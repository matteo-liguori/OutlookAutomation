"""The viewer's Live test page: recent runs from the report folder, read-only."""

from datetime import UTC, datetime

from viewer_rig import Rig

from chamberlain.livetest.model import RunReport, ScenarioResult, Status, compute_score
from chamberlain.livetest.report import write_report

RUN = "20261124T153000-ab12"


def a_report() -> RunReport:
    results = (
        ScenarioResult(id="mail.self_send[mail:a]", group="mail", status=Status.PASS, seconds=2.0),
        ScenarioResult(
            id="mail.reply_thread[mail:a>mail:b]",
            group="mail",
            status=Status.FAIL,
            reason="the reply did not arrive",
        ),
        ScenarioResult(
            id="calendar.recurring[calendar:a]", group="calendar", status=Status.SKIPPED
        ),
        ScenarioResult(
            id="mail.outbox_queued", group="mail", status=Status.SKIPPED, reason="manual scenario"
        ),
    )
    return RunReport(
        run_id=RUN,
        started_at="2026-11-24T15:30:00Z",
        finished_at="2026-11-24T15:35:00Z",
        mode="live",
        results=results,
        score=compute_score(results, 0),
    )


def test_the_page_lists_recent_runs_with_their_score(rig: Rig) -> None:
    assert rig.call("GET", "/api/live-test").body == {"runs": []}
    write_report(rig.ctx.data_root, a_report(), datetime(2026, 11, 24, 16, 0, tzinfo=UTC))
    body = rig.call("GET", "/api/live-test").body
    [run] = body["runs"]
    assert run["run_id"] == RUN
    assert run["score"]["passed"] == 1
    assert run["score"]["failed"] == 1
    reasons = {r["id"]: r["reason"] for r in run["results"]}
    assert reasons["mail.reply_thread[mail:a>mail:b]"] == "the reply did not arrive"
    assert "calendar.recurring[calendar:a]" not in reasons  # a skip without a reason is noise


def test_the_page_script_is_served_and_needs_the_viewers_own_host(rig: Rig) -> None:
    served = rig.call("GET", "/static/livetest.js", token=None)
    assert served.status == 200
    refused = rig.call("GET", "/static/livetest.js", token=None, host="evil.example")
    assert refused.status == 403


def test_the_api_needs_the_token(rig: Rig) -> None:
    assert rig.call("GET", "/api/live-test", token=None).status == 403
