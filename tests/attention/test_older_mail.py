# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
"""A span of more than a day is read for the last day only; the owner decides about the rest.

Contract: docs/contracts/07-attention.md (A-23). Yes: the older mail is read over the next ticks and
the question ends. No: it stays unread and visible, and a later yes still works.
"""

from datetime import timedelta
from typing import Any

import pytest

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.attention import tick
from chamberlain.attention.handlers import source_checks
from chamberlain.attention.tuning import MAIL_STANDARD
from chamberlain.connectors.fake import FakeProvider
from chamberlain.ports.clock import ManualClock

SOURCE = "mail:test"


def go(ctx: Context, clock: ManualClock, times: int = 1) -> None:
    for _ in range(times):
        clock.advance(seconds=61)
        tick.tick(ctx, force=True)


def older_check(ctx: Context) -> Any:
    """The `doctor sources` line about mail held back as too old."""
    return next(c for c in source_checks(ctx) if c.id == "sources.older_mail")


def seen(ctx: Context) -> set[str]:
    rows = ctx.db.execute("SELECT message_key FROM mail_seen WHERE source_id = ?", (SOURCE,))
    return {r["message_key"] for r in rows}


@pytest.fixture
def after_a_long_absence(
    ctx: Context, seed: Any, fake: FakeProvider, clock: ManualClock
) -> tuple[Context, ManualClock]:
    """Watched once, then nobody looked for four days; mail kept arriving the whole time."""
    go(ctx, clock)
    clock.advance(hours=96)
    for n in range(6):
        fake.mail.add_message(
            SOURCE,
            internet_message_id=f"<old{n}@x>",
            date=clock.now() - timedelta(days=3, hours=n),
            folder_id="f-archive" if n % 2 else "f-inbox",
        )
    for n in range(2):
        fake.mail.add_message(
            SOURCE, internet_message_id=f"<new{n}@x>", date=clock.now() - timedelta(hours=2 + n)
        )
    return ctx, clock


def test_a_the_last_day_is_read_at_once_and_the_older_part_is_asked_about(
    after_a_long_absence: tuple[Context, ManualClock], app: App
) -> None:
    ctx, clock = after_a_long_absence
    go(ctx, clock)
    assert seen(ctx) == {"new0@x", "new1@x"}
    held = ctx.db.execute("SELECT * FROM mail_watermarks").fetchone()
    assert held["held_state"] == "asking"
    assert held["held_to"] == (clock.now() - MAIL_STANDARD).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = app.call("start", {})
    item = next(w for w in start.data.work if w.kind.value == "source.stale")
    assert held["held_from"][:10] in item.summary
    assert "about 6" in item.summary, "the owner is told roughly how much mail it is"
    assert item.next.tool.value == "doctor", "the work item never carries the yes for the agent"


def test_a_yes_reads_the_older_mail_over_the_next_ticks_and_ends_the_question(
    after_a_long_absence: tuple[Context, ManualClock], app: App
) -> None:
    ctx, clock = after_a_long_absence
    go(ctx, clock)
    start = app.call("start", {})
    session = str(start.data.session)
    item = next(w for w in start.data.work if w.kind.value == "source.stale")
    answered = app.call(
        "next", {"session": session, "handled": [item.event], "older_mail": [item.event]}
    )
    assert answered.ok
    go(ctx, clock, 4)
    assert len(seen(ctx)) == 8
    state = ctx.db.execute("SELECT * FROM mail_watermarks").fetchone()
    assert state["held_from"] is None
    assert state["gap_from"] is None
    live = ctx.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'source.stale' AND state IN ('new','delivered')"
    ).fetchone()[0]
    assert live == 0
    assert any(r.kind == "mail.older_yes" for r in ctx.services.audit.rows)
    assert older_check(ctx).status == "ok"


def test_a_no_leaves_the_older_mail_unread_and_visible_and_a_later_yes_still_works(
    after_a_long_absence: tuple[Context, ManualClock], app: App
) -> None:
    ctx, clock = after_a_long_absence
    go(ctx, clock)
    start = app.call("start", {})
    session = str(start.data.session)
    item = next(w for w in start.data.work if w.kind.value == "source.stale")
    app.call("next", {"session": session, "handled": [item.event]})
    go(ctx, clock, 6)
    assert seen(ctx) == {"new0@x", "new1@x"}, "a no reads nothing more"
    again = app.call("start", {})
    assert not [w for w in again.data.work if w.kind.value == "source.stale"], "it does not nag"
    assert older_check(ctx).status == "warn", "the uncovered span stays visible"
    assert any(r.kind == "mail.older_no" for r in ctx.services.audit.rows)

    app.call("next", {"session": session, "older_mail": [item.event]})
    go(ctx, clock, 4)
    assert len(seen(ctx)) == 8
    assert ctx.db.execute("SELECT held_from FROM mail_watermarks").fetchone()[0] is None
