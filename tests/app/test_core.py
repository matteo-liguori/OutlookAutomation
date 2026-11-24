"""App.call: docs/contracts/02-tools.md (T-3, T-5, T-16), 03-envelope-errors.md (E-2, E-14)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from chamberlain.app.context import Context, Handler, Result
from chamberlain.app.core import App
from chamberlain.app.doctor import make_doctor
from chamberlain.app.doubles import RecordingAuditLog
from chamberlain.app.wiring import TRACK_MODULES, build_app, load_tracks
from chamberlain.limits import MAX_REPLY_CHARS
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import GuidanceItem, NextStep, Reply
from chamberlain.schema.errors import ChamberlainError, ErrorCategory, ErrorCode
from chamberlain.schema.registry import ToolSpec
from chamberlain.schema.tools import (
    GuideRequest,
    GuideResponse,
    GuideRule,
    StartRequest,
    StartResponse,
    ToolRequest,
)

SESSION = "ses_0123456789abcdef"
OPERATION = "op_0123456789abcdef"


def start(ctx: Context, request: StartRequest) -> Result:
    step = NextStep(tool=ToolName.NEXT, args={"limit": 5}, why="See what is waiting.")
    return Result(StartResponse(session=SESSION, now=ctx.clock.now()), next=(step,))


def app_with(ctx: Context, **handlers: Handler) -> App:
    return App(ctx, {ToolName(name): handler for name, handler in handlers.items()})


def test_a_handler_result_becomes_an_ok_envelope(ctx: Context) -> None:
    reply = app_with(ctx, start=start).call("start", {"agent": "test"})
    assert reply.ok
    assert isinstance(reply.data, StartResponse)
    assert reply.data.now == datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    assert reply.next[0].tool is ToolName.NEXT


def test_unknown_tool_and_bad_request_are_invalid_requests(ctx: Context) -> None:
    app = app_with(ctx, start=start)
    unknown = app.call("briefing", {})
    assert unknown.error is not None
    assert unknown.error.code is ErrorCode.REQUEST_UNKNOWN_TOOL
    bad = app.call("start", {"purpose": "world domination"})
    assert bad.error is not None
    assert bad.error.category is ErrorCategory.INVALID_REQUEST
    assert bad.error.field == "purpose"
    assert bad.error.example is not None


def test_t3_tools_need_a_session_except_start_guide_doctor(ctx: Context) -> None:
    reply = app_with(ctx).call("do", {"operation": OPERATION})
    assert reply.error is not None
    assert reply.error.code is ErrorCode.SESSION_REQUIRED


def test_a_tool_without_a_handler_is_unsupported_not_a_crash(ctx: Context) -> None:
    reply = app_with(ctx).call("do", {"operation": OPERATION, "session": SESSION})
    assert reply.error is not None
    assert reply.error.code is ErrorCode.TOOL_NOT_IMPLEMENTED
    assert reply.error.category is ErrorCategory.UNSUPPORTED


def test_the_session_reaches_the_handler_through_the_context(ctx: Context) -> None:
    seen: list[str | None] = []

    def guide(inner: Context, request: GuideRequest) -> Result:
        seen.append(inner.session)
        return Result(GuideResponse())

    app_with(ctx, guide=guide).call("guide", {"session": SESSION})
    assert seen == [SESSION]


def test_e14_a_chamberlain_error_keeps_its_code_and_anything_else_is_internal(
    ctx: Context,
) -> None:
    def refuses(inner: Context, request: ToolRequest) -> Result:
        raise ChamberlainError(ErrorCode.GRANT_REQUIRED, "No grant covers this.")

    def crashes(inner: Context, request: ToolRequest) -> Result:
        raise ZeroDivisionError("secret internal detail")

    refused = app_with(ctx, start=refuses).call("start", {})
    assert refused.error is not None
    assert refused.error.code is ErrorCode.GRANT_REQUIRED
    crashed = app_with(ctx, start=crashes).call("start", {})
    assert crashed.error is not None
    assert crashed.error.code is ErrorCode.INTERNAL_UNEXPECTED
    assert "secret" not in crashed.model_dump_json()


def test_e2_an_oversized_reply_is_replaced_not_truncated(ctx: Context) -> None:
    def guide(inner: Context, request: GuideRequest) -> Result:
        rules = tuple(
            GuideRule(id=f"rule.number{i}", version=1, title="t", text="x" * 500) for i in range(20)
        )
        return Result(GuideResponse(rules=rules))

    reply = app_with(ctx, guide=guide).call("guide", {})
    assert reply.error is not None
    assert reply.error.code is ErrorCode.REPLY_TOO_LARGE
    assert len(reply.model_dump_json()) < MAX_REPLY_CHARS


def test_e4_a_handler_cannot_return_an_invalid_next_step(ctx: Context) -> None:
    def start_badly(inner: Context, request: StartRequest) -> Result:
        step = NextStep(tool=ToolName.DO, args={"operation": "not a handle"}, why="")
        return Result(StartResponse(session=SESSION, now=inner.clock.now()), next=(step,))

    reply = app_with(ctx, start=start_badly).call("start", {})
    assert reply.error is not None
    assert reply.error.code is ErrorCode.REQUEST_INVALID


class _Hooks:
    def __init__(self, fail_tick: bool = False) -> None:
        self.ticks = 0
        self.fail_tick = fail_tick

    def tick(self, ctx: Context) -> None:
        self.ticks += 1
        if self.fail_tick:
            raise RuntimeError("a detector blew up")

    def decorate(
        self, ctx: Context, spec: ToolSpec, request: ToolRequest, reply: Reply[Model]
    ) -> Reply[Model]:
        item = GuidanceItem(id="start.first", version=1, text="Work through what is due.")
        return reply.model_copy(update={"guidance": (item,)})


def test_t5_a19_every_call_ticks_first_and_is_decorated_after(ctx: Context) -> None:
    hooks = _Hooks()
    reply = App(ctx, {ToolName.START: start}, hooks).call("start", {})
    assert hooks.ticks == 1
    assert [item.id for item in reply.guidance] == ["start.first"]


def test_a7_a_failing_tick_does_not_fail_the_tool(ctx: Context) -> None:
    hooks = _Hooks(fail_tick=True)
    reply = App(ctx, {ToolName.START: start}, hooks).call("start", {})
    assert reply.ok
    assert hooks.ticks == 1


def test_g23_every_call_writes_one_audit_row_without_content(ctx: Context) -> None:
    audit = RecordingAuditLog()
    audited = replace(ctx, services=replace(ctx.services, audit=audit))
    app = app_with(audited, start=start)
    app.call("start", {"agent": "private-agent-label"})
    app.call("do", {"operation": OPERATION, "session": SESSION})
    assert [row.summary for row in audit.rows] == ["start: ok", "do: tool.not_implemented"]
    assert audit.rows[1].actor == f"agent:{SESSION}"
    assert "private-agent-label" not in repr(audit.rows)


def test_doctor_reports_the_schema_checks(ctx: Context) -> None:
    reply = App(ctx, {ToolName.DOCTOR: make_doctor()}).call("doctor", {"check": "schema"})
    assert reply.ok
    dumped = reply.model_dump(mode="json")["data"]["checks"]
    assert {check["id"]: check["status"] for check in dumped} == {
        "schema.migrations": "ok",
        "schema.table_classes": "ok",
    }


def test_wiring_collects_the_track_modules(clock: ManualClock) -> None:
    handlers, hooks = load_tracks()
    assert ToolName.DOCTOR in handlers
    assert hooks is not None
    assert len(TRACK_MODULES) == 3


def test_build_app_opens_a_store_under_the_data_root(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("root") / "data"
    app = build_app(root, clock=ManualClock())
    try:
        assert app.call("doctor", {}).ok
    finally:
        app.close()
    assert (root / "chamberlain.sqlite3").exists()
