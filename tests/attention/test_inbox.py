# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""The event inbox: docs/contracts/07-attention.md (A-1 to A-4), with property tests."""

import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chamberlain.app.context import Context
from chamberlain.attention import inbox, repo
from chamberlain.attention.models import EventAttrs, EventDraft
from chamberlain.domain.events import EventKind
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import MEMORY, connect
from chamberlain.store.migrate import migrate

ACTION = frozenset({EventKind.ACTION_DUE})


def draft(n: int, discriminator: str | None = None) -> EventDraft:
    return EventDraft(
        EventKind.ACTION_DUE,
        f"act_{n:016x}",
        f"Action {n} is due.",
        70,
        EventAttrs(),
        discriminator=discriminator,
    )


def reconcile(db: sqlite3.Connection, clock: ManualClock, drafts: list[EventDraft]) -> None:
    inbox.reconcile(db, clock, drafts, evaluated=ACTION | {EventKind.GRANT_REVIEW_DUE})


def live(db: sqlite3.Connection) -> list[str]:
    return sorted(e.dedupe_key for e in repo.live_events(db))


def test_a1_a_condition_detected_twice_is_one_event(db: sqlite3.Connection, clock: ManualClock):
    reconcile(db, clock, [draft(1)])
    reconcile(db, clock, [draft(1), draft(1)])
    assert len(live(db)) == 1


def test_a1_the_dedupe_key_adds_a_discriminator(clock: ManualClock):
    assert draft(1).dedupe_key == "action.due:act_0000000000000001"
    assert draft(1, "x").dedupe_key.endswith(":x")
    orphans = EventDraft(EventKind.STORAGE_ORPHANS, None, "s", 30)
    assert orphans.dedupe_key == "storage.orphans:-"


def test_a2_an_event_expires_when_its_condition_clears(db: sqlite3.Connection, clock: ManualClock):
    reconcile(db, clock, [draft(1)])
    reconcile(db, clock, [])
    assert live(db) == []
    row = db.execute("SELECT state FROM events").fetchone()
    assert row["state"] == "expired"


def test_a2_delivery_never_closes_an_event(ctx: Context, clock: ManualClock):
    reconcile(ctx.db, clock, [draft(1)])
    inbox.deliver(ctx.db, clock, "ses_aaaaaaaaaaaaaaaa", repo.live_events(ctx.db))
    assert [e.state.value for e in repo.live_events(ctx.db)] == ["delivered"]


def test_a2_a_kind_whose_detector_failed_is_left_alone(db: sqlite3.Connection, clock: ManualClock):
    reconcile(db, clock, [draft(1)])
    inbox.reconcile(db, clock, [], evaluated=frozenset({EventKind.GRANT_NEEDED}))
    assert len(live(db)) == 1


def test_a2_mail_events_of_an_unreadable_source_are_kept(
    db: sqlite3.Connection, clock: ManualClock
):
    kinds = frozenset({EventKind.MAIL_NEW_UNMATCHED})
    mail = EventDraft(
        EventKind.MAIL_NEW_UNMATCHED,
        "msg_00000000000000a1",
        "New mail.",
        40,
        EventAttrs(source="mail:test"),
    )
    inbox.reconcile(db, clock, [mail], evaluated=kinds)
    inbox.reconcile(db, clock, [], evaluated=kinds, unreadable_sources=frozenset({"mail:test"}))
    assert len(live(db)) == 1
    inbox.reconcile(db, clock, [], evaluated=kinds)
    assert live(db) == []


def handle_all(db: sqlite3.Connection, clock: ManualClock) -> list[str]:
    handles = [e.handle for e in repo.live_events(db)]
    return inbox.handle_events(db, clock, handles)


def test_a3_a_handled_event_does_not_reappear_while_still_true(
    db: sqlite3.Connection, clock: ManualClock
):
    reconcile(db, clock, [draft(1)])
    handle_all(db, clock)
    clock.advance(hours=48)
    reconcile(db, clock, [draft(1)])
    assert live(db) == []


def test_a3_it_returns_once_the_condition_has_been_false(
    db: sqlite3.Connection, clock: ManualClock
):
    reconcile(db, clock, [draft(1)])
    handle_all(db, clock)
    reconcile(db, clock, [])
    reconcile(db, clock, [draft(1)])
    assert len(live(db)) == 1


def test_a3_a_review_due_grant_comes_back_after_seven_days(
    db: sqlite3.Connection, clock: ManualClock
):
    grant = EventDraft(EventKind.GRANT_REVIEW_DUE, "grant_0000000000000001", "g", 60)
    reconcile(db, clock, [grant])
    handle_all(db, clock)
    clock.advance(hours=144)
    reconcile(db, clock, [grant])
    assert live(db) == []
    clock.advance(hours=48)
    reconcile(db, clock, [grant])
    assert len(live(db)) == 1


def test_a4_an_uncertain_operation_cannot_be_handled_by_the_agent(
    db: sqlite3.Connection, clock: ManualClock
):
    op = EventDraft(EventKind.OPERATION_UNCERTAIN, "op_0000000000000001", "o", 95)
    inbox.reconcile(db, clock, [op], evaluated=frozenset({EventKind.OPERATION_UNCERTAIN}))
    assert handle_all(db, clock) == []
    assert len(live(db)) == 1


def test_a4_unknown_event_handles_change_nothing(db: sqlite3.Connection, clock: ManualClock):
    reconcile(db, clock, [draft(1)])
    real = repo.live_events(db)[0].handle
    with pytest.raises(ChamberlainError) as raised:
        inbox.handle_events(db, clock, [real, "evt_ffffffffffffffff"])
    assert raised.value.code is ErrorCode.REQUEST_UNKNOWN_HANDLE
    assert len(live(db)) == 1


# --- properties ----------------------------------------------------------------

KEYS = range(4)
operation = st.one_of(
    st.tuples(st.just("detect"), st.frozensets(st.sampled_from(KEYS))),
    st.tuples(st.just("deliver"), st.just(frozenset[int]())),
    st.tuples(st.just("handle"), st.just(frozenset[int]())),
    st.tuples(st.just("wait"), st.just(frozenset[int]())),
)


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(st.lists(operation, max_size=30))
def test_a1_a3_random_sequences_never_duplicate_or_lose_a_true_condition(
    steps: list[tuple[str, frozenset[int]]],
):
    clock = ManualClock()
    db = connect(MEMORY)
    migrate(db, clock, backup_dir=None)
    true_now: frozenset[int] = frozenset()
    handled_at_true: set[int] = set()
    try:
        for name, keys in steps:
            if name == "detect":
                true_now = keys
                reconcile(db, clock, [draft(k) for k in sorted(keys)])
                handled_at_true &= set(keys)
            elif name == "deliver":
                inbox.deliver(db, clock, "ses_aaaaaaaaaaaaaaaa", repo.live_events(db))
            elif name == "handle":
                for event in repo.live_events(db):
                    inbox.handle_events(db, clock, [event.handle])
                    handled_at_true.add(int((event.subject or "")[-16:], 16))
            else:
                clock.advance(hours=30)
            keys_live = [e.dedupe_key for e in repo.live_events(db)]
            assert len(keys_live) == len(set(keys_live))
            for k in true_now:
                key = draft(k).dedupe_key
                assert key in keys_live or k in handled_at_true
            for k in KEYS:
                if k not in true_now:
                    assert draft(k).dedupe_key not in keys_live
    finally:
        db.close()
