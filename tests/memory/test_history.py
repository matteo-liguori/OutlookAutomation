"""Weekly stored-bytes history: docs/contracts/10-targets.md (4, 12 and 26 weeks)."""

from chamberlain.app.context import Context
from chamberlain.memory.history import (
    HISTORY_SLOTS,
    load,
    record_snapshot,
    stored_bytes_history,
)
from chamberlain.ports.clock import ManualClock


def snap(ctx: Context, clock: ManualClock) -> bool:
    return record_snapshot(ctx.db, ctx.clock, ctx.data_root, clock.now())


def test_one_snapshot_a_week_with_the_figures_the_contract_names(
    ctx: Context, clock: ManualClock
) -> None:
    assert snap(ctx, clock)
    clock.advance(hours=24 * 3)
    assert not snap(ctx, clock)
    clock.advance(hours=24 * 4)
    assert snap(ctx, clock)
    first = load(ctx.db)[0]
    assert set(first) == {"at", "bytes", "rows", "cards", "card_avg"}
    assert isinstance(first["rows"], dict)


def test_history_reports_checkpoints_only_when_enough_weeks_exist(
    ctx: Context, clock: ManualClock
) -> None:
    assert stored_bytes_history(ctx.db).week_4 is None
    for _ in range(5):
        snap(ctx, clock)
        clock.advance(hours=24 * 7)
    report = stored_bytes_history(ctx.db)
    assert report.weeks_recorded == 5
    assert isinstance(report.week_4, int)
    assert report.week_12 is None
    assert report.week_26 is None
    for _ in range(25):
        snap(ctx, clock)
        clock.advance(hours=24 * 7)
    report = stored_bytes_history(ctx.db)
    assert isinstance(report.week_26, int)


def test_at_most_thirty_snapshots_are_kept(ctx: Context, clock: ManualClock) -> None:
    for _ in range(HISTORY_SLOTS + 5):
        snap(ctx, clock)
        clock.advance(hours=24 * 7)
    assert len(load(ctx.db)) == HISTORY_SLOTS
