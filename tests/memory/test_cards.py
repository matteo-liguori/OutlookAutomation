"""Cards, actions and notes: docs/contracts/06-memory.md (M-12 to M-16, lifetime table)."""

import pytest
from memory_helpers import keep_text

from chamberlain.app.context import Context
from chamberlain.limits import (
    ACTION_TITLE_MAX,
    CARD_GOAL_MAX,
    CARD_MAX_CHARS,
    CARD_POSITION_MAX,
    CARD_RULES_MAX,
    CARD_TITLE_MAX,
    NOTE_MAX,
    NOTES_PER_ACTION_MAX,
    OUTCOME_MAX,
)
from chamberlain.memory import repo
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.memory.cards import CardService
from chamberlain.memory.checks import storage_checks
from chamberlain.memory.evidence import EvidenceService
from chamberlain.memory.metrics import memory_metrics
from chamberlain.memory.sweeper import sweep
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode


def refused(call: object, code: ErrorCode) -> ChamberlainError:
    assert callable(call)
    with pytest.raises(ChamberlainError) as raised:
        call()
    assert raised.value.code is code
    return raised.value


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("title", CARD_TITLE_MAX),
        ("goal", CARD_GOAL_MAX),
        ("position", CARD_POSITION_MAX),
        ("rules", CARD_RULES_MAX),
    ],
)
def test_m12_each_field_cap_refuses_and_names_field_size_and_limit(
    cards: CardService, field: str, limit: int
) -> None:
    card = cards.create(title="Short")
    error = refused(
        lambda: cards.update(card.handle, None, {field: "x" * (limit + 1)}), ErrorCode.CARD_OVER_CAP
    )
    assert error.field == field
    assert str(limit) in error.fix
    assert str(limit + 1) in error.message
    assert getattr(cards.view(card.handle), field) == getattr(card, field)  # nothing truncated


def test_m12_the_whole_open_card_is_capped(cards: CardService) -> None:
    card = cards.create(
        title="t" * CARD_TITLE_MAX,
        goal="g" * CARD_GOAL_MAX,
        position="p" * CARD_POSITION_MAX,
        rules="r" * CARD_RULES_MAX,
    )
    error: ChamberlainError | None = None
    for index in range(7):
        try:
            cards.add_action(
                card.handle, None, title="a" * ACTION_TITLE_MAX, due_at=None, snooze_until=None
            )
        except ChamberlainError as raised:
            error = raised
            break
        assert index < 6
    assert error is not None
    assert error.code is ErrorCode.CARD_OVER_CAP
    assert error.field == "card"
    assert str(CARD_MAX_CHARS) in error.fix


def test_m12_open_actions_per_card_are_capped_at_seven(cards: CardService) -> None:
    card = cards.create(title="Many things")
    for index in range(7):
        cards.add_action(card.handle, None, title=f"Step {index}", due_at=None, snooze_until=None)
    error = refused(
        lambda: cards.add_action(
            card.handle, None, title="One too many", due_at=None, snooze_until=None
        ),
        ErrorCode.CARD_OVER_CAP,
    )
    assert error.field == "actions"
    finish = cards.view(card.handle).actions[0].handle
    cards.finish_action(finish, None, "Done.")
    cards.add_action(card.handle, None, title="Now it fits", due_at=None, snooze_until=None)


def test_m12_action_title_and_outcome_caps(cards: CardService, action: str) -> None:
    card = cards.create(title="Another")
    refused(
        lambda: cards.add_action(
            card.handle, None, title="t" * (ACTION_TITLE_MAX + 1), due_at=None, snooze_until=None
        ),
        ErrorCode.CARD_OVER_CAP,
    )
    refused(
        lambda: cards.finish_action(action, None, "o" * (OUTCOME_MAX + 1)), ErrorCode.CARD_OVER_CAP
    )
    refused(
        lambda: cards.close(card.handle, None, "o" * (OUTCOME_MAX + 1)), ErrorCode.CARD_OVER_CAP
    )


def test_m12_notes_are_capped_in_size_and_number(cards: CardService, action: str) -> None:
    refused(lambda: cards.add_note(action, "n" * (NOTE_MAX + 1)), ErrorCode.NOTE_OVER_CAP)
    for index in range(NOTES_PER_ACTION_MAX):
        cards.add_note(action, f"note {index}")
    error = refused(lambda: cards.add_note(action, "the sixth"), ErrorCode.NOTE_OVER_CAP)
    assert "older note" in error.fix


def test_m12_evidence_is_capped(ctx: Context, cards: CardService) -> None:
    card = cards.create(title="Evidence card")
    service = EvidenceService(ctx.db, ctx.clock)
    refused(
        lambda: service.add(text="e" * 2001, owner=card.handle, session=None),
        ErrorCode.EVIDENCE_OVER_CAP,
    )
    service.add(text="e" * 2000, owner=card.handle, session=None)


def test_m13_an_imported_card_over_cap_is_flagged_and_must_come_under(cards: CardService) -> None:
    card = cards.create(title="Imported", goal="g" * 900, imported=True)
    assert card.over_cap
    assert card.review_at is not None
    refused(
        lambda: cards.update(card.handle, None, {"rules": "still big"}), ErrorCode.CARD_OVER_CAP
    )
    fixed = cards.update(card.handle, None, {"goal": "short"})
    assert not fixed.over_cap
    assert fixed.review_at is None


def test_m14_updating_replaces_and_never_appends(cards: CardService) -> None:
    card = cards.create(title="Plan", position="first position")
    updated = cards.update(card.handle, None, {"position": "second position"})
    assert updated.position == "second position"
    assert "first" not in updated.model_dump_json()


def test_m16_a_stale_version_conflicts(cards: CardService) -> None:
    card = cards.create(title="Versioned")
    assert card.version == 1
    second = cards.update(card.handle, 1, {"goal": "a goal"})
    assert second.version == 2
    error = refused(
        lambda: cards.update(card.handle, 1, {"goal": "stale write"}),
        ErrorCode.CARD_VERSION_CONFLICT,
    )
    assert "version 2" in error.message


def test_m3_closing_compacts_and_erases_everything_the_card_held(
    ctx: Context, cards: CardService, store: ArtifactService, clock: ManualClock
) -> None:
    card = cards.create(title="To close", goal="goal", position="position", rules="rules")
    view = cards.add_action(card.handle, None, title="Open task", due_at=None, snooze_until=None)
    action = view.actions[0].handle
    note = cards.add_note(action, "scratch")
    artifact = keep_text(store, action, "held body")
    evidence = EvidenceService(ctx.db, ctx.clock).add(
        text="User said yes.", owner=card.handle, session=None
    )
    repo.link_card(ctx.db, card.handle, "msg_aaaaaaaaaaaa", None, "2026-01-05T09:00:00Z")
    closed = cards.close(card.handle, None, "Settled with the supplier.")
    assert (closed.state, closed.outcome) == ("closed", "Settled with the supplier.")
    assert (closed.goal, closed.position, closed.rules) == ("", "", "")
    assert closed.actions == ()
    assert repo.get_note(ctx.db, note) is None
    assert repo.get_evidence(ctx.db, evidence) is None
    assert repo.cards_for_ref(ctx.db, "msg_aaaaaaaaaaaa") == []
    assert (
        ctx.db.execute("SELECT state FROM artifacts WHERE handle = ?", (artifact,)).fetchone()[
            "state"
        ]
        == "released"
    )
    ended = repo.get_action(ctx.db, action)
    assert ended is not None
    assert ended["state"] == "dropped"
    sweep(ctx, clock.now())
    assert store_files(ctx) == 0


def store_files(ctx: Context) -> int:
    return len([p for p in (ctx.data_root / "artifacts").rglob("*") if p.is_file()])


def test_m3_reopening_starts_empty_and_outcome_lines_age_out(
    ctx: Context, cards: CardService, clock: ManualClock
) -> None:
    card = cards.create(title="Reopen me", position="old position")
    cards.close(card.handle, None, "Closed for now.")
    reopened = cards.reopen(card.handle, None)
    assert reopened.state == "open"
    assert (reopened.goal, reopened.position, reopened.rules, reopened.actions) == ("", "", "", ())
    assert reopened.outcome is None
    second = cards.create(title="Ages out")
    cards.close(second.handle, None, "All done.")
    clock.advance(hours=24 * 364)
    sweep(ctx, clock.now())
    assert repo.get_card(ctx.db, second.handle) is not None
    clock.advance(hours=24 * 2)
    report = sweep(ctx, clock.now())
    assert repo.get_card(ctx.db, second.handle) is None
    assert report.total("cards_purged") == 1
    assert repo.get_card(ctx.db, card.handle) is not None  # open cards are never purged


def test_m3_finished_action_loses_notes_and_its_line_goes_after_90_days(
    ctx: Context, cards: CardService, action: str, clock: ManualClock
) -> None:
    note = cards.add_note(action, "scratch")
    card = repo.card_of_action(ctx.db, action)
    assert card is not None
    view = cards.finish_action(action, None, "Quote sent.")
    assert view.actions == ()
    assert repo.get_note(ctx.db, note) is None
    row = repo.get_action(ctx.db, action)
    assert row is not None
    assert (row["state"], row["outcome"]) == ("done", "Quote sent.")
    clock.advance(hours=24 * 91)
    sweep(ctx, clock.now())
    assert repo.get_action(ctx.db, action) is None


def test_m3_a_note_untouched_for_30_days_goes_even_with_its_action_open(
    ctx: Context, cards: CardService, action: str, clock: ManualClock
) -> None:
    note = cards.add_note(action, "stale scratch")
    clock.advance(hours=24 * 29)
    sweep(ctx, clock.now())
    assert repo.get_note(ctx.db, note) is not None
    clock.advance(hours=24 * 2)
    sweep(ctx, clock.now())
    assert repo.get_note(ctx.db, note) is None


def test_t10_a_note_needs_an_open_action(cards: CardService, action: str) -> None:
    card = cards.create(title="Not an action")
    refused(lambda: cards.add_note(card.handle, "text"), ErrorCode.ARTIFACT_NO_OWNER)
    cards.finish_action(action, None, "Done.")
    refused(lambda: cards.add_note(action, "text"), ErrorCode.ARTIFACT_OWNER_ENDED)


def test_m14_actions_can_be_updated_in_place(cards: CardService, action: str) -> None:
    view = cards.update_action(
        action,
        None,
        title="Send the revised quote",
        due_at="2026-02-01T10:00:00Z",
        snooze_until=None,
        set_due=True,
        set_snooze=False,
    )
    assert view.actions[0].title == "Send the revised quote"
    assert view.actions[0].due_at is not None


def test_m12_evidence_target_of_15_per_open_card_is_reported_not_refused(
    ctx: Context, cards: CardService
) -> None:
    card = cards.create(title="Crowded")
    service = EvidenceService(ctx.db, ctx.clock)
    for index in range(15):
        service.add(text=f"Fact {index}.", owner=card.handle, session=None)
    assert {c.id: c.status for c in storage_checks(ctx)}["storage.evidence"] == "ok"
    service.add(text="Fact sixteen.", owner=card.handle, session=None)  # a target, not a refusal
    assert {c.id: c.status for c in storage_checks(ctx)}["storage.evidence"] == "warn"
    metrics = memory_metrics(ctx.db, ctx.clock, ctx.data_root)
    assert metrics.evidence_rows == 16
    assert metrics.evidence_per_open_card == 16.0


def test_t10_targets_metrics_report_open_cards_artifacts_and_ownerless_storage(
    ctx: Context, cards: CardService, store: ArtifactService, action: str, clock: ManualClock
) -> None:
    keep_text(store, action, "twelve bytes")
    metrics = memory_metrics(ctx.db, ctx.clock, ctx.data_root)
    assert metrics.open_cards == 1
    assert 0 < metrics.open_card_chars_avg <= metrics.open_card_chars_max <= CARD_MAX_CHARS
    assert (metrics.artifacts_live, metrics.artifact_bytes) == (1, 12)
    assert metrics.artifacts_without_owner == 0
    cards.finish_action(action, None, "Done.")  # releases the hold; the sweeper has not run yet
    sweep(ctx, clock.now())
    after = memory_metrics(ctx.db, ctx.clock, ctx.data_root)
    assert (after.artifacts_live, after.artifacts_without_owner) == (0, 0)
