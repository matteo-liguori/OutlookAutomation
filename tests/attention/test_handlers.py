# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""`start`, `next` and `guide` through `App.call`: docs/contracts/02-tools.md (T-3, T-7)."""

from typing import Any

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.app.doctor import make_doctor
from chamberlain.attention.handlers import DOCTOR_CHECKS, HANDLERS, HOOKS
from chamberlain.attention.rules import load_rules
from chamberlain.schema.base import ToolName
from chamberlain.schema.errors import ErrorCode


def test_t3_t7_a9_next_needs_a_session_but_start_and_guide_do_not_and_3_related_checks(
    app: App, seed: Any
) -> None:
    # t3_next_needs_a_session_but_start_and_guide_do_not
    assert app.call("start", {}).ok
    assert app.call("guide", {}).ok
    reply = app.call("next", {})
    assert reply.error is not None
    assert reply.error.code is ErrorCode.SESSION_REQUIRED

    # t7_start_lists_sources_with_capabilities
    reply = app.call("start", {"agent": "test"})
    sources = {s.id: s for s in reply.data.sources}
    assert set(sources) == {"mail:test", "calendar:test", "contacts:test"}
    assert "mail.read" in sources["mail:test"].capabilities
    assert sources["mail:test"].state.value == "ok"

    # a9_next_marks_handled_and_does_not_repeat
    seed.grant()
    seed.grant()
    start = app.call("start", {})
    session = str(start.data.session)
    events = [w.event for w in start.data.work]
    reply = app.call("next", {"session": session, "handled": events[:1]})
    assert reply.ok
    assert reply.data.work == ()

    # a9_next_refuses_unknown_event_handles
    session = str(app.call("start", {}).data.session)
    reply = app.call("next", {"session": session, "handled": ["evt_ffffffffffffffff"]})
    assert reply.error is not None
    assert reply.error.code is ErrorCode.REQUEST_UNKNOWN_HANDLE


def test_a4_a17_t7_e13_a12_the_agent_cannot_close_an_uncertain_operation_and_3_related_checks(
    app: App, seed: Any, ctx: Context
) -> None:
    # a4_the_agent_can_close_an_unsettled_write_once_and_it_stays_closed
    seed.operation()
    start = app.call("start", {})
    event = start.data.work[0].event
    session = str(start.data.session)
    app.call("next", {"session": session, "handled": [event]})
    again = app.call("start", {"agent": "second"})
    assert again.data.work == ()

    # a17_t7_e13_guide_text_ledger_index_and_refusals
    # a17_guide_returns_the_full_text_and_records_a_delivery
    session = str(app.call("start", {}).data.session)
    reply = app.call("guide", {"session": session, "id": "write.unsettled"})
    rule = reply.data.rules[0]
    assert rule.text is not None
    assert "ask the user" in rule.text
    row = ctx.db.execute(
        "SELECT version FROM guidance_deliveries WHERE rule_id = 'write.unsettled'"
    ).fetchone()
    assert row is not None

    # a17_guide_ignores_the_ledger
    session = str(app.call("start", {}).data.session)
    for _ in range(2):
        reply = app.call("guide", {"session": session, "id": "session.first"})
        assert reply.data.rules[0].text

    # t7_the_guide_index_is_paged_and_carries_no_text
    first = app.call("guide", {})
    assert first.coverage is not None
    assert not first.coverage.complete
    assert all(r.text is None for r in first.data.rules)
    second = app.call("guide", {"cursor": first.coverage.cursor})
    seen = [r.id for r in first.data.rules] + [r.id for r in second.data.rules]
    assert seen == [r.id for r in load_rules()] + ["setup.agent"]
    assert all(r.text is None for r in second.data.rules)
    assert second.coverage is not None
    assert second.coverage.complete

    # e13_an_unknown_rule_and_a_bad_cursor_are_refused_with_a_fix
    unknown = app.call("guide", {"id": "no.such.rule"})
    assert unknown.error is not None
    assert unknown.error.fix
    bad = app.call("guide", {"cursor": "nonsense"})
    assert bad.error is not None
    assert bad.error.code is ErrorCode.REQUEST_BAD_CURSOR

    # a12_doctor_reports_guidance_and_sources
    app = App(ctx, {**HANDLERS, ToolName.DOCTOR: make_doctor(DOCTOR_CHECKS)}, HOOKS)
    reply = app.call("doctor", {"check": "guidance"})
    results = {c.id: c for c in reply.data.checks}
    assert results["guidance.rules"].status == "ok"
    assert results["guidance.overrides"].status == "ok"
    sources = app.call("doctor", {"check": "sources"})
    assert sources.data.checks[0].id == "sources.health"

    # golden_guide_by_id_and_the_first_index_page
    one = app.call("guide", {"id": "do.once"})
    assert one.model_dump(mode="json", exclude_none=True) == {
        "ok": True,
        "data": {
            "rules": [
                {
                    "id": "do.once",
                    "version": 2,
                    "title": "Carrying out a change",
                    "text": one.data.rules[0].text,
                }
            ]
        },
        "next": [],
        "guidance": [],
        "notices": [],
    }
    assert "Run it once." in one.data.rules[0].text
    index = app.call("guide", {})
    assert [r.id for r in index.data.rules][:3] == [
        "action.due",
        "appointments.important",
        "calendar.checks",
    ]
    assert index.coverage.total == len(load_rules()) + 1
    assert index.coverage.returned == 25
