# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""Scenario tests: this store state and this call give exactly these rules and notices.

Contract: docs/contracts/07-attention.md (A-9, A-15, A-16, A-19) and 02-tools.md (T-7).
"""

import re
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from chamberlain.app.context import Context, Result, SourceRouter
from chamberlain.app.core import App
from chamberlain.attention import repo, tick
from chamberlain.attention.handlers import HANDLERS, HOOKS
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.calendar import EventFields, Response
from chamberlain.domain.capabilities import HealthState
from chamberlain.limits import MAX_REPLY_CHARS
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import Coverage, CoverageGap, Reply
from chamberlain.schema.registry import check_next_step
from chamberlain.schema.tools import FindRequest, FindResponse

HANDLE = re.compile(r"\b(ses|evt|op|grant|card|act|msg)_[0-9a-f]{16}\b")


def normalise(value: Any) -> Any:
    if isinstance(value, str):
        return HANDLE.sub(lambda m: f"<{m.group(1)}>", value)
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


def dump(reply: Reply[Model]) -> dict[str, Any]:
    return normalise(reply.model_dump(mode="json", exclude_none=True))


def ids(reply: Reply[Model]) -> list[str]:
    return [g.id for g in reply.guidance]


class Find:
    """A stand-in for the memory track's `find`, so other tools can carry notices."""

    complete = True

    def __call__(self, ctx: Context, request: FindRequest) -> Result:
        if self.complete:
            return Result(FindResponse(), coverage=Coverage(complete=True, returned=0))
        gap = CoverageGap(source="mail:test", reason="not synchronised")
        return Result(FindResponse(), coverage=Coverage(complete=False, returned=0, gaps=(gap,)))


@pytest.fixture
def find() -> Find:
    return Find()


@pytest.fixture
def full_app(ctx: Context, seed: Any, find: Find) -> App:
    return App(ctx, {**HANDLERS, ToolName.FIND: find}, HOOKS)


def test_t7_a15_a_first_start_with_nothing_waiting_and_1_related_checks(full_app: App) -> None:
    # t7_a_first_start_with_nothing_waiting
    reply = full_app.call("start", {"agent": "test"})
    assert reply.ok
    assert reply.data is not None
    assert ids(reply) == ["session.first"]
    assert reply.data.work == ()
    assert reply.notices == ()

    # a15_a_scheduled_start_adds_the_scheduled_rule_first
    reply = full_app.call("start", {"agent": "test", "purpose": "scheduled"})
    assert ids(reply) == ["session.scheduled", "session.first"]


def test_a2_an_uncertain_send_is_work_and_brings_its_rule(full_app: App, seed: Any):
    op = seed.operation("mail.send")
    reply = full_app.call("start", {"agent": "test"})
    assert ids(reply) == ["write.unsettled", "session.first"]
    assert reply.data is not None
    work = reply.data.work
    assert [w.kind.value for w in work] == ["operation.uncertain"]
    assert work[0].next.tool is ToolName.OPEN
    assert work[0].next.args == {"handle": op, "part": "summary"}
    assert reply.next[0] == work[0].next


def test_a15_an_uncertain_calendar_write_gets_the_general_rule(full_app: App, seed: Any):
    seed.operation("calendar.create")
    reply = full_app.call("start", {"agent": "test"})
    assert ids(reply) == ["write.unsettled", "session.first"]


def test_a15_a_grant_needing_the_user_brings_its_rule(full_app: App, seed: Any):
    seed.grant(requirement="human_confirmation")
    reply = full_app.call("start", {"agent": "test"})
    assert ids(reply) == ["grant.confirm", "session.first"]


def test_a15_an_agent_evidence_grant_is_not_raised(full_app: App, seed: Any):
    seed.grant(requirement="agent_evidence")
    reply = full_app.call("start", {"agent": "test"})
    assert reply.data.work == ()


def test_e3_incomplete_coverage_brings_the_coverage_rule(full_app: App, find: Find, seed: Any):
    session = str(full_app.call("start", {}).data.session)
    find.complete = False
    reply = full_app.call("find", {"session": session, "scope": "mail"})
    assert ids(reply) == ["coverage.incomplete", "mail.read_state"]
    again = full_app.call("find", {"session": session, "scope": "mail"})
    assert ids(again) == []


def test_a15_a_storage_budget_passed_brings_its_rule(full_app: App, seed: Any):
    seed.system("memory.over_budget", "total:600/500")
    reply = full_app.call("start", {})
    assert ids(reply) == ["session.first", "storage.budget"]
    assert [w.kind.value for w in reply.data.work] == ["storage.over_budget"]


def test_a1_a_card_over_its_budget_is_its_own_event(full_app: App, seed: Any):
    card = seed.card()
    seed.system("memory.over_budget", f"{card}:30/20,total:600/500")
    reply = full_app.call("start", {})
    assert len(reply.data.work) == 2


def test_t7_start_returns_at_most_five_items_and_counts_the_rest(full_app: App, seed: Any):
    for _ in range(8):
        seed.grant()
    reply = full_app.call("start", {})
    assert len(reply.data.work) == 5
    assert reply.data.more_work == 3
    assert "body" not in reply.model_dump_json()
    second = full_app.call("next", {"session": reply.data.session, "limit": 10})
    assert len(second.data.work) == 3
    assert second.data.remaining == 0


def test_e4_a19_every_work_item_names_a_valid_call_and_1_related_checks(
    full_app: App, seed: Any, clock: ManualClock
) -> None:
    # e4_every_work_item_names_a_valid_call
    card = seed.card(review_at=seed.ts(hours=-1))
    seed.card(over_cap=1)
    seed.action(card, due_at=seed.ts(hours=-1))
    seed.action(card, snooze_until=seed.ts(hours=-1))
    seed.grant()
    seed.grant(state="active", review_at=seed.ts(hours=-1))
    seed.operation()
    seed.system("memory.over_budget", "total:600/500")
    seed.system("memory.last_orphan_scan_at", seed.ts())
    seed.orphans(seed.ts())
    start = full_app.call("start", {})
    more = full_app.call("next", {"session": start.data.session, "limit": 10})
    items = [*start.data.work, *more.data.work]
    kinds = {w.kind.value for w in items}
    assert kinds == {
        "operation.uncertain",
        "grant.needed",
        "grant.review_due",
        "action.due",
        "reminder.due",
        "card.review_due",
        "storage.over_budget",
        "storage.orphans",
    }
    for item in items:
        check_next_step(item.next)

    # a19_notices_piggyback_urgent_events_on_other_tools
    session = str(full_app.call("start", {}).data.session)
    op = seed.operation("mail.send")
    clock.advance(seconds=120)
    reply = full_app.call("find", {"session": session, "scope": "mail"})
    assert [n.kind.value for n in reply.notices] == ["operation.uncertain"]
    assert reply.notices[0].next is not None
    assert reply.notices[0].next.args == {"handle": op, "part": "summary"}
    assert reply.next == (reply.notices[0].next,)
    again = full_app.call("find", {"session": session, "scope": "mail"})
    assert again.notices == ()


def test_a19_a_decorated_reply_stays_within_the_cap(full_app: App, seed: Any):
    for _ in range(20):
        seed.operation()
    reply = full_app.call("start", {})
    assert len(reply.model_dump_json(exclude_none=True)) <= MAX_REPLY_CHARS


def test_a3_handled_work_does_not_return(full_app: App, seed: Any, clock: ManualClock):
    seed.grant()
    start = full_app.call("start", {})
    session = str(start.data.session)
    event = start.data.work[0].event
    handled = full_app.call("next", {"session": session, "handled": [event]})
    assert handled.data.work == ()
    clock.advance(seconds=120)
    fresh = full_app.call("start", {"agent": "other"})
    assert fresh.data.work == ()


def test_a2_a_resumed_session_is_not_shown_the_same_item_twice(full_app: App, seed: Any):
    seed.grant()
    first = full_app.call("start", {"agent": "x"})
    again = full_app.call("start", {"agent": "x", "resume": first.data.session})
    assert again.data.session == first.data.session
    assert again.data.work == ()
    assert "grant.confirm" not in ids(again)


def test_golden_an_uncertain_send_start_reply(full_app: App, seed: Any):
    seed.operation("mail.send")
    reply = full_app.call("start", {"agent": "test"})
    golden = dump(reply)
    golden["data"].pop("now")
    assert golden["data"]["sources"] == [
        {
            "id": "calendar:test",
            "kind": "calendar",
            "state": "ok",
            "capabilities": golden["data"]["sources"][0]["capabilities"],
        },
        {
            "id": "contacts:test",
            "kind": "contacts",
            "state": "ok",
            "capabilities": golden["data"]["sources"][1]["capabilities"],
        },
        {
            "id": "mail:test",
            "kind": "mail",
            "state": "ok",
            "capabilities": golden["data"]["sources"][2]["capabilities"],
        },
    ]
    assert golden["data"]["work"] == [
        {
            "event": "<evt>",
            "kind": "operation.uncertain",
            "subject": "<op>",
            "summary": "mail.send <op> is still unsettled after a day; "
            "the harness could not find it.",
            "next": {
                "tool": "open",
                "args": {"handle": "<op>", "part": "summary"},
                "why": "Read what it was meant to do, then look for it yourself. "
                "Never do it twice.",
            },
        }
    ]
    assert golden["data"]["session"] == "<ses>"
    assert golden["data"]["more_work"] == 0
    assert golden["ok"] is True
    assert [g["id"] for g in golden["guidance"]] == ["write.unsettled", "session.first"]
    assert golden["guidance"][0]["version"] >= 1
    assert golden["next"][0]["tool"] == "open"


def test_golden_next_after_handling_returns_the_rest_and_1_related_checks(
    full_app: App, seed: Any, clock: Any
) -> None:
    # golden_next_after_handling_returns_the_rest
    seed.grant()
    seed.card(over_cap=1)
    start = full_app.call("start", {"agent": "g"})
    session = start.data.session
    first = start.data.work[0].event
    reply = full_app.call("next", {"session": session, "handled": [first], "limit": 3})
    assert dump(reply) == {
        "ok": True,
        "data": {"work": [], "remaining": 0},
        "next": [],
        "guidance": [],
        "notices": [],
    }

    # golden_next_shows_a_new_event_as_one_exact_call
    session = full_app.call("start", {"agent": "g"}).data.session
    seed.operation("calendar.update")
    clock.advance(seconds=120)
    reply = full_app.call("next", {"session": session})
    assert dump(reply)["data"] == {
        "work": [
            {
                "event": "<evt>",
                "kind": "operation.uncertain",
                "subject": "<op>",
                "summary": "calendar.update <op> is still unsettled after a day; "
                "the harness could not find it.",
                "next": {
                    "tool": "open",
                    "args": {"handle": "<op>", "part": "summary"},
                    "why": "Read what it was meant to do, then look for it yourself. "
                    "Never do it twice.",
                },
            }
        ],
        "remaining": 0,
    }
    assert ids(reply) == ["write.unsettled"]


def test_a26_two_events_that_overlap_are_raised_with_their_rule_and_clear_when_apart(
    full_app: App, ctx: Context, fake: FakeProvider, clock: ManualClock, seed: Any
):
    day = clock.now() + timedelta(days=2)

    def fields(title: str, start_hour: int, hours: int = 1, **more: Any) -> EventFields:
        start = day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        return EventFields(title=title, start=start, end=start + timedelta(hours=hours), **more)

    calendar = fake.calendar
    calendar.add_source("calendar:second")
    seed.source("calendar:second")
    ctx = replace(
        ctx,
        sources=SourceRouter(
            mail=ctx.sources.mail,
            calendar={"calendar:test": calendar, "calendar:second": calendar},
            contacts=ctx.sources.contacts,
        ),
    )
    app = App(ctx, HANDLERS, HOOKS)
    # Things that look like clashes and are not: a copy held by two calendars, an all-day
    # entry, a declined invitation, an event that only touches the next one.
    calendar.add_event("calendar:test", fields("Standup", 9), uid="same")
    calendar.add_event("calendar:second", fields("Standup", 9), uid="same")
    calendar.add_event("calendar:test", fields("Holiday", 0, 24, all_day=True))
    calendar.add_event(
        "calendar:test",
        fields("Declined", 9),
        organiser="boss@x.example",
        my_response=Response.DECLINED,
    )
    calendar.add_event("calendar:test", fields("Before", 8))
    assert app.call("start", {}).data.work == ()

    # A real clash across two calendars.
    late = calendar.add_event("calendar:second", fields("Dentist", 9, 2))
    clock.advance(minutes=2)
    reply = app.call("start", {})
    work = reply.data.work
    assert [w.kind.value for w in work] == ["calendar.conflict"]
    assert work[0].next.tool is ToolName.FIND
    assert work[0].next.args["scope"] == "calendar"
    assert "Dentist" not in work[0].summary
    assert "calendar.conflict" in ids(reply)

    # A calendar that cannot be read says nothing, so the event stays.
    fake.world.health_override["calendar:second"] = HealthState.UNAVAILABLE
    tick.tick(ctx, force=True)
    assert "calendar.conflict" in [e.kind.value for e in repo.live_events(ctx.db)]
    del fake.world.health_override["calendar:second"]

    # Moving it away clears the event on the next tick.
    fake.world.events["calendar:second"][late].fields = fields("Dentist", 14)
    tick.tick(ctx, force=True)
    assert [e.kind.value for e in repo.live_events(ctx.db)] == []

    # With a calendar kept for information only (commitments: false) the overlap is only a
    # possible clash, left to the agent's judgement; two information-only calendars never clash.
    fake.world.events["calendar:second"][late].fields = fields("Dentist", 9, 2)
    informational = replace(
        ctx, sources=replace(ctx.sources, information_only=frozenset({"calendar:second"}))
    )
    tick.tick(informational, force=True)
    [possible] = [e for e in repo.live_events(ctx.db) if e.kind.value == "calendar.conflict"]
    assert possible.attrs.reason == "possible"
    assert possible.summary.startswith("Possible clash")
    both = replace(
        ctx,
        sources=replace(
            ctx.sources, information_only=frozenset({"calendar:second", "calendar:test"})
        ),
    )
    tick.tick(both, force=True)
    assert [e.kind.value for e in repo.live_events(ctx.db)] == []
