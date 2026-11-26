# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""The tick and the detectors: docs/contracts/07-attention.md (A-2 to A-8)."""

from dataclasses import replace
from typing import Any

import pytest

from chamberlain.app.context import Context, TickSteps
from chamberlain.app.core import App
from chamberlain.attention import repo, tick
from chamberlain.attention.collect import collect
from chamberlain.attention.detectors import DETECTORS
from chamberlain.attention.models import (
    DetectorInput,
    MailBatch,
    SourceSnapshot,
)
from chamberlain.connectors.fake import FakeProvider, Fault
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind
from chamberlain.memory.repo import ActionRow, CardRow
from chamberlain.ports.clock import ManualClock
from chamberlain.writes.repo import GrantAlert as GrantRow
from chamberlain.writes.repo import OperationAlert as OperationRow


def kinds(ctx: Context) -> list[str]:
    return sorted(e.kind.value for e in repo.live_events(ctx.db))


def test_a5_a2_a4_a1_g8_every_detector_is_pure_and_raises_exactly_its_and_1_related_checks(
    clock: ManualClock, ctx: Context, seed: Any, fake: FakeProvider, app: App
) -> None:
    # a5_a2_a4_a1_g8_m26_every_detector_is_pure_and_raises_exactly_its_events
    # a5_there_is_exactly_one_detector_per_kind
    assert set(DETECTORS) == set(EventKind)

    # a5_detectors_are_pure_functions_of_their_input
    data = DetectorInput(
        now=clock.now(),
        operations=(OperationRow("op_0000000000000001", "mail.send", "uncertain"),),
    )
    first = [d for fn in DETECTORS.values() for d in fn(data)]
    assert first == [d for fn in DETECTORS.values() for d in fn(data)]
    assert [d.kind for d in first] == [EventKind.OPERATION_UNCERTAIN]

    # a5_wave_two_detectors_report_nothing
    data = DetectorInput(now=clock.now())
    for kind in (
        EventKind.CALENDAR_CONFLICT,
        EventKind.USER_NOTE,
        EventKind.UPDATE_AVAILABLE,
        EventKind.SCHEDULE_PASS_DUE,
    ):
        assert list(DETECTORS[kind](data)) == []

    # a2_snoozed_work_is_silent_until_it_is_due
    soon = (clock.now().replace(hour=12)).isoformat()
    data = DetectorInput(
        now=clock.now(),
        actions=(
            ActionRow(
                "act_0000000000000001", "card_0000000000000001", "2026-01-01T00:00:00Z", soon
            ),
        ),
    )
    assert list(DETECTORS[EventKind.ACTION_DUE](data)) == []
    assert list(DETECTORS[EventKind.REMINDER_DUE](data)) == []

    # a2_a_woken_snooze_is_a_reminder_not_a_due_action
    past = "2026-01-01T00:00:00Z"
    data = DetectorInput(
        now=clock.now(),
        actions=(ActionRow("act_0000000000000001", "card_0000000000000001", None, past),),
    )
    assert len(list(DETECTORS[EventKind.REMINDER_DUE](data))) == 1
    assert list(DETECTORS[EventKind.ACTION_DUE](data)) == []

    # g8_review_due_and_over_cap_cards
    data = DetectorInput(
        now=clock.now(),
        cards=(
            CardRow("card_0000000000000001", "t", "2026-01-01T00:00:00Z", False),
            CardRow("card_0000000000000002", "t", None, True),
        ),
    )
    reasons = sorted(d.attrs.reason or "" for d in DETECTORS[EventKind.CARD_REVIEW_DUE](data))
    assert reasons == ["over_cap", "review"]

    # m26_orphans_are_one_event_per_scan
    data = DetectorInput(now=clock.now(), orphan_batch="2026-01-05T08:00:00Z", orphan_count=3)
    found = list(DETECTORS[EventKind.STORAGE_ORPHANS](data))
    assert len(found) == 1
    assert found[0].dedupe_key.endswith("2026-01-05T08:00:00Z")

    # a4_sign_in_and_stale_are_per_source
    data = DetectorInput(
        now=clock.now(),
        sources=(
            SourceSnapshot("mail:a", HealthState.SIGN_IN_NEEDED),
            SourceSnapshot("mail:b", HealthState.STALE),
            SourceSnapshot("mail:c", HealthState.OK),
        ),
    )
    assert [d.subject for d in DETECTORS[EventKind.SOURCE_SIGN_IN_NEEDED](data)] == ["mail:a"]
    assert [d.subject for d in DETECTORS[EventKind.SOURCE_STALE](data)] == ["mail:b"]

    # a1_new_mail_is_one_batch_per_source_and_for_card_is_retired
    data = DetectorInput(
        now=clock.now(),
        mail=(
            MailBatch("mail:a", 3, 7, "2026-01-01T00:00:00Z", 1),
            MailBatch("mail:b", 1, 9, "2026-01-02T00:00:00Z", 0),
        ),
    )
    drafts = list(DETECTORS[EventKind.MAIL_NEW_UNMATCHED](data))
    assert [(d.subject, d.discriminator) for d in drafts] == [("mail:a", "7"), ("mail:b", "9")]
    assert "1 with a suggested card" in drafts[0].summary
    assert "mail.new_for_card" not in {kind.value for kind in DETECTORS}

    # g8_grants_waiting_and_due
    data = DetectorInput(
        now=clock.now(),
        grants=(GrantRow("grant_0000000000000001", "proposed", "human_confirmation", None),),
        review_grants=(GrantRow("grant_0000000000000002", "expired", "none", None),),
    )
    assert len(list(DETECTORS[EventKind.GRANT_NEEDED](data))) == 1
    assert len(list(DETECTORS[EventKind.GRANT_REVIEW_DUE](data))) == 1

    # a6_new_mail_becomes_events_and_stays_unread
    locator = fake.mail.add_message("mail:test", internet_message_id="<m1@x>", subject="Hi there")
    tick.tick(ctx, force=True)
    assert kinds(ctx) == ["mail.new_unmatched"]
    assert fake.mail.is_unread("mail:test", locator)


# --- through the tick ------------------------------------------------------------


def test_a2_mail_already_read_elsewhere_is_still_seen_once_and_stays_until_handled(
    ctx: Context, seed: Any, fake: FakeProvider, clock: ManualClock
):
    fake.mail.add_message("mail:test", internet_message_id="<m1@x>", unread=False)
    tick.tick(ctx, force=True)
    assert kinds(ctx) == ["mail.new_unmatched"]
    fake.world.mail["mail:test"][0].unread = False
    clock.advance(seconds=120)
    tick.tick(ctx, force=True)
    assert kinds(ctx) == ["mail.new_unmatched"]


def test_a6_a_tick_within_a_minute_is_skipped(ctx: Context, seed: Any, clock: ManualClock):
    assert tick.tick(ctx) is True
    seed.operation()
    clock.advance(seconds=30)
    assert tick.tick(ctx) is False
    assert kinds(ctx) == []
    clock.advance(seconds=31)
    assert tick.tick(ctx) is True
    assert kinds(ctx) == ["operation.uncertain"]


def test_a6_a_tick_already_running_elsewhere_is_not_started_again(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
):
    """Another caller arriving while a tick collects skips it (found live: eight at once)."""
    real = collect
    inner: list[bool] = []

    def collect_while_another_caller_arrives(context: Context) -> Any:
        inner.append(tick.tick(context))
        return real(context)

    monkeypatch.setattr(tick, "collect", collect_while_another_caller_arrives)
    assert tick.tick(ctx) is True
    assert inner == [False]


def test_w9_recovery_steps_run_even_on_a_skipped_tick(ctx: Context, clock: ManualClock):
    calls: list[str] = []
    steps = TickSteps(
        always=(lambda c: calls.append("recover"),), extra=(lambda c: calls.append("sweep"),)
    )
    ctx = replace(ctx, tick_steps=steps)
    tick.tick(ctx)
    clock.advance(seconds=5)
    tick.tick(ctx)
    assert calls == ["recover", "sweep", "recover"]
    other: list[str] = []
    tick.tick(replace(ctx, tick_steps=TickSteps(always=(lambda c: other.append("x"),))))
    assert calls == ["recover", "sweep", "recover"]
    assert other == ["x"]


def test_a7_a_failing_step_is_logged_and_the_tick_goes_on(ctx: Context, seed: Any):
    def boom(c: Context) -> None:
        raise RuntimeError("step failed")

    ctx = replace(ctx, tick_steps=TickSteps(always=(boom,), extra=(boom,)))
    seed.operation()
    assert tick.tick(ctx) is True
    assert kinds(ctx) == ["operation.uncertain"]


def test_a7_a_failing_detector_leaves_its_events_and_the_call_works(
    ctx: Context, seed: Any, app: App, monkeypatch: pytest.MonkeyPatch, clock: ManualClock
):
    seed.grant()
    tick.tick(ctx, force=True)
    assert kinds(ctx) == ["grant.needed"]

    def broken(data: DetectorInput) -> list[Any]:
        raise RuntimeError("detector failed")

    monkeypatch.setitem(DETECTORS, EventKind.GRANT_NEEDED, broken)
    seed.operation()
    clock.advance(seconds=120)
    reply = app.call("start", {})
    assert reply.ok
    assert kinds(ctx) == ["grant.needed", "operation.uncertain"]


def test_a7_a_source_that_raises_never_fails_the_call(
    ctx: Context, seed: Any, fake: FakeProvider, app: App, monkeypatch: pytest.MonkeyPatch
):
    def raising(*args: object, **kwargs: object) -> object:
        raise RuntimeError("source failed")

    monkeypatch.setattr(fake.mail, "search", raising)
    monkeypatch.setattr(fake.calendar, "health", raising)
    reply = app.call("start", {})
    assert reply.ok
    assert "source.stale" in kinds(ctx)


def test_a2_mail_events_survive_a_stale_read_of_their_source(
    ctx: Context, seed: Any, fake: FakeProvider, clock: ManualClock
):
    fake.mail.add_message("mail:test", internet_message_id="<m1@x>")
    tick.tick(ctx, force=True)
    fake.arm(Fault.STALE, times=5)
    tick.tick(ctx, force=True)
    assert "mail.new_unmatched" in kinds(ctx)
    assert "source.stale" in kinds(ctx)
    fake.disarm()
    tick.tick(ctx, force=True)
    assert kinds(ctx) == ["mail.new_unmatched"]


def test_a2_source_health_events_are_per_source_and_clear_on_recovery(
    ctx: Context, seed: Any, fake: FakeProvider
) -> None:
    # a2_a_source_failure_clears_when_it_recovers
    fake.world.health_override["mail:test"] = HealthState.SIGN_IN_NEEDED
    tick.tick(ctx, force=True)
    assert kinds(ctx) == ["source.sign_in_needed"]
    del fake.world.health_override["mail:test"]
    tick.tick(ctx, force=True)
    assert kinds(ctx) == []

    # a2_a_calendar_problem_does_not_touch_mail_sources
    fake.world.health_override["calendar:test"] = HealthState.UNAVAILABLE
    tick.tick(ctx, force=True)
    events = repo.live_events(ctx.db)
    assert [(e.kind.value, e.subject) for e in events] == [("source.stale", "calendar:test")]


def test_a6_delivery_wakes_only_when_no_session_is_active(ctx: Context, seed: Any, app: App):
    seed.grant()
    tick.tick(ctx, force=True)
    assert tick.wake_status(ctx) == tick.WAKE_EXIT_STATUS
    app.call("start", {})
    assert tick.wake_status(ctx) == 0
