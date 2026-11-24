"""The tick: collect, detect, reconcile, then the other tracks' steps.

Contract: docs/contracts/07-attention.md (A-6, A-7). A tick never raises.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from chamberlain.app.context import Context
from chamberlain.attention import inbox, repo, sessions
from chamberlain.attention.collect import collect
from chamberlain.attention.detectors import DETECTORS
from chamberlain.attention.models import EventDraft
from chamberlain.domain.events import EventKind
from chamberlain.limits import TICK_MIN_INTERVAL_SECONDS
from chamberlain.store.db import transaction
from chamberlain.store.schema import set_state

log = logging.getLogger("chamberlain.attention")

TickStep = Callable[[Context], None]
"""A step another track contributes. It does its own transaction and may raise."""

ALWAYS_STEPS: list[TickStep] = []
"""Run on every call, even when the tick is skipped: crashed-operation recovery (W-9)."""

EXTRA_STEPS: list[TickStep] = []
"""Run after the inbox is reconciled, at most once per interval: the sweeper (M-21)."""

WAKE_EXIT_STATUS = 10


def _run_steps(ctx: Context, steps: list[TickStep]) -> None:
    for step in steps:
        try:
            step(ctx)
        except Exception:
            log.exception("tick step %s failed", getattr(step, "__name__", "step"))


def _due(ctx: Context) -> bool:
    last = repo.last_tick(ctx.db)
    if last is None:
        return True
    elapsed = ctx.clock.now() - datetime.fromisoformat(last)
    return elapsed.total_seconds() >= TICK_MIN_INTERVAL_SECONDS


def tick(ctx: Context, *, force: bool = False) -> bool:
    """Run one tick. Returns True when detectors ran, False when it was skipped."""
    _run_steps(ctx, ALWAYS_STEPS)
    try:
        if ctx.session is not None:
            sessions.touch(ctx.db, ctx.clock, ctx.session)
        sessions.end_idle(ctx.db, ctx.clock)
    except Exception:
        log.exception("session upkeep failed")
    try:
        if not force and not _due(ctx):
            return False
        collected = collect(ctx)
        drafts: list[EventDraft] = []
        evaluated: set[EventKind] = set()
        for kind, detector in DETECTORS.items():
            try:
                found = list(detector(collected.data))
            except Exception:
                log.exception("detector %s failed", kind.value)
                continue
            drafts.extend(found)
            evaluated.add(kind)
        inbox.reconcile(
            ctx.db,
            ctx.clock,
            drafts,
            evaluated=frozenset(evaluated),
            unreadable_sources=collected.unreadable,
        )
        with transaction(ctx.db):
            set_state(ctx.db, ctx.clock, "attention.last_tick_at", _stamp(ctx))
        _run_steps(ctx, EXTRA_STEPS)
        return True
    except Exception:
        log.exception("tick failed")
        return False


def _stamp(ctx: Context) -> str:
    return ctx.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


def wake_status(ctx: Context) -> int:
    """10 when events wait for a session and none is active, else 0 (A-6, Delivery 3)."""
    waiting = repo.count_undelivered(ctx.db) > 0
    return WAKE_EXIT_STATUS if waiting and not repo.any_active_session(ctx.db) else 0
