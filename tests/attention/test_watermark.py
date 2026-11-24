# pyright: reportAttributeAccessIssue=false
"""The new-mail watermark: no message is skipped across ticks, restarts, bursts and faults.

Contract: docs/contracts/07-attention.md (A-6, A-7). Random histories over the fake
provider with a small page size so every run pages, resumes and recovers.
"""

import dataclasses
import random
from datetime import timedelta
from typing import Any

import pytest

from chamberlain.app.context import Context
from chamberlain.attention import tick, watermark
from chamberlain.connectors.fake import FakeProvider
from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.capabilities import HealthState
from chamberlain.ports.clock import ManualClock

SOURCE = "mail:test"


def seen(ctx: Context) -> set[str]:
    rows = ctx.db.execute("SELECT locator FROM mail_seen WHERE source_id = ?", (SOURCE,))
    return {r["locator"] for r in rows}


def row(ctx: Context) -> Any:
    return ctx.db.execute("SELECT * FROM mail_watermarks WHERE source_id = ?", (SOURCE,)).fetchone()


def kinds(ctx: Context) -> list[str]:
    rows = ctx.db.execute(
        "SELECT kind FROM events WHERE state IN ('new', 'delivered') ORDER BY kind"
    )
    return [r["kind"] for r in rows]


@pytest.fixture(autouse=True)
def _small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watermark, "MAIL_PAGE_LIMIT", 7)
    monkeypatch.setattr(watermark, "MAIL_MAX_PAGES", 2)


def arm_page_two_failure(fake: FakeProvider) -> None:
    """The next search that asks for a second page raises, once."""
    real = fake.mail.search
    state = {"armed": True}

    def search(source: str, query: Any, cursor: str | None = None) -> Any:
        if cursor is not None and state["armed"]:
            state["armed"] = False
            raise RuntimeError("connection dropped")
        return real(source, query, cursor)

    fake.mail.search = search  # type: ignore[method-assign]


def run_tick(ctx: Context, clock: ManualClock) -> None:
    clock.advance(seconds=61)
    tick.tick(ctx, force=True)


@pytest.mark.parametrize("seed", range(60))
def test_a_no_message_is_ever_skipped(
    seed: int, ctx: Context, fake: FakeProvider, clock: ManualClock, seed_sources: None
) -> None:
    rng = random.Random(seed)
    added: dict[str, object] = {}
    counter = 0
    for _ in range(rng.randint(20, 50)):
        step = rng.choice(
            [
                "add",
                "add",
                "burst",
                "tick",
                "tick",
                "read",
                "wait",
                "stale",
                "raise",
                "restart",
                "page2",
            ]
        )
        if step in ("add", "burst"):
            for _ in range(1 if step == "add" else rng.randint(8, 40)):
                counter += 1
                late = timedelta(minutes=rng.choice([0, 0, 0, 1, 5]))
                locator = fake.mail.add_message(
                    SOURCE,
                    internet_message_id=f"<m{counter}@x>",
                    date=clock.now()
                    - late
                    - timedelta(seconds=counter // 3, milliseconds=counter % 3 * 7),
                    unread=rng.random() < 0.7,
                )
                added[locator] = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        elif step == "tick":
            run_tick(ctx, clock)
        elif step == "read":
            for stored in fake.world.mail[SOURCE]:
                stored.unread = False
        elif step == "wait":
            clock.advance(hours=rng.choice([1, 8, 30]))
        elif step == "restart":
            fake.restart()
            ctx = dataclasses.replace(ctx)
        elif step == "page2":
            arm_page_two_failure(fake)
        elif step == "stale":
            fake.arm(Fault.STALE, times=rng.randint(1, 3))
        else:
            fake.world.health_override[SOURCE] = HealthState.UNAVAILABLE
        if step == "wait" and rng.random() < 0.5:
            fake.world.health_override.pop(SOURCE, None)

        # After a tick: whatever the watermark then claims covered has been seen.
        state = row(ctx)
        if (
            step == "tick"
            and state is not None
            and state["scan_started_at"] is None
            and state["gap_from"] is None
        ):
            through = state["covered_through"]
            for stored in fake.world.mail[SOURCE]:
                locator = f"{stored.folder_id}/{stored.internet_message_id}"
                arrived = str(added[locator])
                if arrived < through and stored.date >= clock.now() - timedelta(days=3):
                    assert locator in seen(ctx), (seed, locator)

    fake.disarm()
    fake.world.health_override.clear()
    for _ in range(80):
        run_tick(ctx, clock)
        if seen(ctx) == set(added) and row(ctx)["gap_from"] is None:
            break
    assert row(ctx)["gap_from"] is None
    missing = set(added) - seen(ctx)
    detail = [
        (
            added[f"{s.folder_id}/{s.internet_message_id}"],
            s.internet_message_id,
            s.date.isoformat(),
            s.folder_id,
        )
        for s in fake.world.mail[SOURCE]
        if f"{s.folder_id}/{s.internet_message_id}" in missing
    ]
    assert not missing, (seed, str(clock.now()), dict(row(ctx)), detail[:5], len(missing))
    assert seen(ctx) == set(added)
    assert "source.stale" not in kinds(ctx)


@pytest.fixture
def seed_sources(ctx: Context) -> None:
    for source in ("mail:test", "calendar:test", "contacts:test"):
        now = clock_stamp(ctx)
        ctx.db.execute(
            "INSERT OR IGNORE INTO sources (id, kind, connector, created_at, updated_at) "
            "VALUES (?, ?, 'fake', ?, ?)",
            (source, source.split(":")[0], now, now),
        )


def clock_stamp(ctx: Context) -> str:
    return ctx.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_mail_read_on_the_phone_is_still_seen_once(
    ctx: Context, fake: FakeProvider, clock: ManualClock, seed_sources: None
) -> None:
    locator = fake.mail.add_message(SOURCE, internet_message_id="<a@x>", unread=False)
    run_tick(ctx, clock)
    assert seen(ctx) == {locator}
    run_tick(ctx, clock)
    assert len(seen(ctx)) == 1


def test_a_backlog_over_the_page_budget_resumes_and_names_the_gap(
    ctx: Context, fake: FakeProvider, clock: ManualClock, seed_sources: None
) -> None:
    run_tick(ctx, clock)
    clock.advance(minutes=10)
    for n in range(40):
        fake.mail.add_message(
            SOURCE, internet_message_id=f"<b{n}@x>", date=clock.now() - timedelta(seconds=n * 7)
        )
    run_tick(ctx, clock)
    first = len(seen(ctx))
    assert 0 < first < 40
    stale = ctx.db.execute("SELECT summary FROM events WHERE kind = 'source.stale'").fetchone()
    assert stale is not None
    assert "is not covered yet" in stale["summary"]
    for _ in range(10):
        run_tick(ctx, clock)
        if row(ctx)["gap_from"] is None:
            break
    assert len(seen(ctx)) == 40
    assert "source.stale" not in kinds(ctx)


def test_a_a_never_watched_source_starts_a_recorded_lookback_back_and_not_further(
    ctx: Context, fake: FakeProvider, clock: ManualClock, seed_sources: None
) -> None:
    within = fake.mail.add_message(
        SOURCE, internet_message_id="<in@x>", date=clock.now() - timedelta(hours=40)
    )
    before = fake.mail.add_message(
        SOURCE, internet_message_id="<out@x>", date=clock.now() - timedelta(hours=60)
    )
    run_tick(ctx, clock)
    assert seen(ctx) == {within}
    assert before not in seen(ctx)
    assert row(ctx)["watching_since"] == clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_an_unreachable_source_raises_stale_with_the_exact_span_since_the_watermark(
    ctx: Context, fake: FakeProvider, clock: ManualClock, seed_sources: None
) -> None:
    run_tick(ctx, clock)
    through = row(ctx)["covered_through"]
    fake.world.health_override[SOURCE] = HealthState.UNAVAILABLE
    clock.advance(hours=5)
    run_tick(ctx, clock)
    assert row(ctx)["covered_through"] == through
    start = through.replace("T", " ")[:16]
    end = clock.now().strftime("%Y-%m-%d %H:%M")
    summary = ctx.db.execute("SELECT summary FROM events WHERE kind = 'source.stale'").fetchone()
    assert summary is not None
    assert f"mail from {start} to {end} UTC is not covered yet" in summary["summary"]
    del fake.world.health_override[SOURCE]
    run_tick(ctx, clock)
    assert row(ctx)["gap_from"] is None
    assert "source.stale" not in kinds(ctx)
