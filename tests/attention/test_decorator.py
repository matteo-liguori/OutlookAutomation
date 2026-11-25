# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""The decorator: docs/contracts/07-attention.md (A-16, A-19) and 03-envelope-errors.md (E-2)."""

from typing import Any

import pytest

from chamberlain.app.context import Context, Result
from chamberlain.app.core import App
from chamberlain.attention import hooks as hooks_module
from chamberlain.attention import repo, tick
from chamberlain.attention.handlers import HANDLERS, HOOKS
from chamberlain.attention.hooks import AttentionHooks
from chamberlain.attention.rules import load_rules
from chamberlain.attention.selection import Situation, select
from chamberlain.limits import GUIDANCE_REPLY_CHARS, MAX_REPLY_CHARS
from chamberlain.schema.base import ToolName
from chamberlain.schema.envelope import ContentKind, ContentLine, Coverage, CoverageGap, Reply
from chamberlain.schema.tools import FindRequest, FindResponse, Hit


def test_a19_it_changes_only_next_guidance_and_notices(ctx: Context, seed: Any):
    seed.operation()
    plain = App(ctx, HANDLERS, None).call("start", {"agent": "a"})
    decorated = App(ctx, HANDLERS, HOOKS).call("start", {"agent": "b"})
    assert plain.ok
    assert decorated.ok
    for field in ("ok", "coverage", "error"):
        assert getattr(plain, field) == getattr(decorated, field)
    assert type(plain.data) is type(decorated.data)
    assert plain.guidance == ()
    assert decorated.guidance


def test_a19_guidance_in_a_reply_stays_within_the_reply_budget(app: App, seed: Any):
    seed.operation("mail.send")
    seed.grant()
    reply = app.call("start", {})
    used = sum(len(g.text or "") for g in reply.guidance)
    assert used <= GUIDANCE_REPLY_CHARS


def test_a19_a_decorator_failure_returns_the_reply_untouched(
    ctx: Context, seed: Any, monkeypatch: pytest.MonkeyPatch
):
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("decorate failed")

    monkeypatch.setattr(hooks_module, "select", broken)
    reply = App(ctx, HANDLERS, HOOKS).call("start", {})
    assert reply.ok
    assert reply.guidance == ()


def test_a19_a_gap_is_not_hidden_by_the_decorator(ctx: Context, seed: Any):
    def partial(c: Context, request: FindRequest) -> Result:
        gap = CoverageGap(source="mail:test", reason="not synchronised")
        return Result(FindResponse(), coverage=Coverage(complete=False, returned=0, gaps=(gap,)))

    app = App(ctx, {**HANDLERS, ToolName.FIND: partial}, HOOKS)
    session = str(app.call("start", {}).data.session)
    reply = app.call("find", {"session": session, "scope": "mail"})
    assert reply.coverage is not None
    assert not reply.coverage.complete


def test_a19_hooks_implement_the_app_protocol():
    assert isinstance(HOOKS, AttentionHooks)
    assert isinstance(Reply[FindResponse](ok=True, data=FindResponse()), Reply)


def test_a16_a_rule_that_does_not_fit_becomes_a_pointer_until_asked_for(ctx: Context, seed: Any):
    """With a tiny budget lower rules turn into pointers, the urgent one stays whole."""
    rules = load_rules()
    uncertain = _event(seed, ctx)
    situation = Situation(
        tool="start",
        request={"purpose": "scheduled"},
        reply={},
        error_code=None,
        error_category=None,
        events=(uncertain,),
    )
    picked = select(rules, situation, {}, budget=300)
    assert picked.full[0].rule.id == "send.uncertain"
    assert picked.pointers
    assert all(p.id not in {c.rule.id for c in picked.full} for p in picked.pointers)
    assert len(picked.pointers) <= 3


def _event(seed: Any, ctx: Context) -> Any:
    seed.operation("mail.send")
    tick.tick(ctx, force=True)
    return repo.live_events(ctx.db)[0]


def test_e2_guidance_drops_to_pointers_before_anything_else(ctx: Context, seed: Any):
    """A handler reply close to the cap loses guidance first, never its data."""

    def big(c: Context, request: FindRequest) -> Result:
        hits = tuple(
            Hit(
                handle=f"msg_{i:016x}",
                kind="message",
                summary="s" * 114,
                content=ContentLine(
                    origin=f"msg_{i:016x}", kind=ContentKind.MAIL_LINE, text="t" * 40
                ),
            )
            for i in range(19)
        )
        return Result(FindResponse(hits=hits), coverage=Coverage(complete=True, returned=19))

    app = App(ctx, {**HANDLERS, ToolName.FIND: big}, HOOKS)
    session = str(app.call("start", {}).data.session)
    plain = App(ctx, {ToolName.FIND: big}, None).call("find", {"session": session, "scope": "mail"})
    reply = app.call("find", {"session": session, "scope": "mail"})
    plain_size = len(plain.model_dump_json(exclude_none=True))
    assert plain.ok
    assert MAX_REPLY_CHARS - 120 < plain_size <= MAX_REPLY_CHARS
    assert reply.ok
    assert len(reply.model_dump_json(exclude_none=True)) <= MAX_REPLY_CHARS
    assert reply.data == plain.data
    assert not any(g.text for g in reply.guidance)
