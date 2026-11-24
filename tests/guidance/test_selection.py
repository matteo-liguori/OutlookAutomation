# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""Selection: docs/contracts/07-attention.md (Selection, A-15, A-16, A-17)."""

from typing import Any

from chamberlain.attention.models import EventAttrs, EventRow
from chamberlain.attention.rules import Rule, load_rules
from chamberlain.attention.selection import Situation, select
from chamberlain.domain.events import EventKind, EventState
from chamberlain.limits import GUIDANCE_POINTER_COST, GUIDANCE_REPLY_CHARS


def rule(rule_id: str, text: str, priority: int, **extra: Any) -> Rule:
    fields: dict[str, Any] = {
        "id": rule_id,
        "version": 1,
        "title": rule_id,
        "when": {"tool": "find"},
        "text": text,
        "max_chars": max(len(text), 1),
        "once_per": "session",
        "priority": priority,
    }
    fields.update(extra)
    return Rule.model_validate(fields)


def situation(tool: str = "find", events: tuple[EventRow, ...] = (), **request: Any) -> Situation:
    return Situation(tool, request, {}, None, None, events)


def uncertain(
    handle: str = "evt_aaaaaaaaaaaaaaaa", subject: str = "op_aaaaaaaaaaaaaaaa"
) -> EventRow:
    return EventRow(
        handle,
        EventKind.OPERATION_UNCERTAIN,
        subject,
        f"k:{handle}",
        95,
        "s",
        EventAttrs(),
        EventState.NEW,
        "2026-01-05T09:00:00Z",
        None,
        None,
        None,
        None,
    )


def ids(picked: Any) -> list[str]:
    return [c.rule.id for c in picked.full]


def test_a15_rules_are_ranked_by_priority_then_id():
    rules = (rule("b.two", "x", 50), rule("a.one", "x", 50), rule("c.top", "x", 80))
    assert ids(select(rules, situation(), {})) == ["c.top", "a.one", "b.two"]


def test_a15_selection_is_deterministic():
    rules = tuple(rule(f"r.{i}", "text " * 10, i * 7 % 40) for i in range(9))
    first = select(rules, situation(), {})
    assert first == select(rules, situation(), {})


def test_selection_step_2_a_delivered_rule_at_this_version_is_dropped():
    rules = (rule("a.one", "x", 50),)
    assert ids(select(rules, situation(), {("a.one", "session"): 1})) == []
    assert ids(select(rules, situation(), {("a.one", "session"): 0})) == ["a.one"]


def test_selection_step_2_a_new_version_is_delivered_again():
    newer = rule("a.one", "x", 50, version=2)
    assert ids(select((newer,), situation(), {("a.one", "session"): 1})) == ["a.one"]


def test_selection_once_per_always_repeats():
    always = rule("a.one", "x", 50, once_per="always")
    assert ids(select((always,), situation(), {("a.one", "session"): 1})) == ["a.one"]


def test_selection_once_per_operation_is_scoped_to_each_operation():
    when = {"event": "operation.uncertain"}
    per_op = rule("a.one", "x", 50, when=when, once_per="operation")
    one, two = uncertain(), uncertain("evt_bbbbbbbbbbbbbbbb", "op_bbbbbbbbbbbbbbbb")
    picked = select(
        (per_op,), situation("start", (one, two)), {("a.one", "op_aaaaaaaaaaaaaaaa"): 1}
    )
    assert [c.scopes for c in picked.full] == [("op_bbbbbbbbbbbbbbbb",)]


def test_selection_step_4_whole_rules_only_never_cut():
    rules = (
        rule("a.big", "y" * 500, 60),
        rule("b.small", "z" * 200, 50),
        rule("c.tiny", "w" * 150, 40),
    )
    picked = select(rules, situation(), {})
    assert ids(picked) == ["a.big", "b.small"]
    assert all(len(c.rule.text) in (500, 200) for c in picked.full)
    assert [r.id for r in picked.pointers] == ["c.tiny"]


def test_selection_step_5_a_rule_that_does_not_fit_becomes_a_pointer():
    rules = (rule("a.big", "y" * 600, 60), rule("b.next", "z" * 300, 50))
    picked = select(rules, situation(), {})
    assert ids(picked) == ["a.big"]
    assert [r.id for r in picked.pointers] == ["b.next"]


def test_selection_pointers_cost_budget_and_are_capped_at_three():
    wide = tuple(rule(f"p.{i}", "z" * 400, 10 + i) for i in range(6))
    assert len(select(wide, situation(), {}).pointers) <= 3
    used = sum(len(c.rule.text) for c in select(wide, situation(), {}).full)
    assert (
        used + GUIDANCE_POINTER_COST * len(select(wide, situation(), {}).pointers)
        <= GUIDANCE_REPLY_CHARS
    )


def test_a16_an_urgent_rule_keeps_its_place_ahead_of_lower_ones():
    urgent = rule("a.urgent", "u" * 600, 95)
    lower = tuple(rule(f"l.{i}", "l" * 150, 60) for i in range(4))
    picked = select((*lower, urgent), situation(), {})
    assert ids(picked)[0] == "a.urgent"
    assert sum(len(c.rule.text) for c in picked.full) <= GUIDANCE_REPLY_CHARS


def test_selection_event_filters_use_the_event_attributes():
    when = {"event": "operation.uncertain", "intent_kind": "mail.send"}
    only_sends = rule("a.sends", "x", 50, when=when)
    plain = situation("start", (uncertain(),))
    assert ids(select((only_sends,), plain, {})) == []


def test_selection_result_and_error_triggers():
    by_result = rule("a.cov", "x", 50, when={"result": "coverage.complete == false"})
    by_error = rule("a.err", "x", 40, when={"error": "not_authorised"})
    sit = Situation(
        "find", {}, {"coverage": {"complete": False}}, "grant.required", "not_authorised", ()
    )
    assert ids(select((by_result, by_error), sit, {})) == ["a.cov", "a.err"]
    ok = Situation("find", {}, {"coverage": {"complete": True}}, None, None, ())
    assert ids(select((by_result, by_error), ok, {})) == []


def test_selection_a_rule_with_no_trigger_is_never_pushed():
    only_fetched = rule("a.only", "x", 99, when={})
    assert ids(select((only_fetched,), situation(), {})) == []


def test_a15_the_start_event_rules_of_the_real_set_fit_the_budget():
    sit = Situation("start", {"purpose": "scheduled"}, {}, None, None, (uncertain(),))
    picked = select(load_rules(), sit, {})
    assert sum(len(c.rule.text) for c in picked.full) <= GUIDANCE_REPLY_CHARS
