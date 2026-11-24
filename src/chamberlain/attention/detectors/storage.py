"""Storage budgets and orphans, as memory records them (M-26, M-27)."""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.events import EventKind


def over_budget(data: DetectorInput) -> Iterable[EventDraft]:
    for budget in data.budgets:
        if budget.scope is None:
            summary = "Stored files have passed the overall budget."
        else:
            summary = f"Card {budget.scope} holds more stored files than its budget."
        yield EventDraft(
            EventKind.STORAGE_OVER_BUDGET,
            budget.scope,
            summary,
            PRIORITY[EventKind.STORAGE_OVER_BUDGET],
            EventAttrs(card=budget.scope, reason="card" if budget.scope else "total"),
        )


def orphans(data: DetectorInput) -> Iterable[EventDraft]:
    """One event per scan that found orphans; a later scan is a new condition (A-3)."""
    if data.orphan_batch is None or data.orphan_count == 0:
        return
    yield EventDraft(
        EventKind.STORAGE_ORPHANS,
        None,
        "Unknown files were set aside and will be deleted after a grace period.",
        PRIORITY[EventKind.STORAGE_ORPHANS],
        EventAttrs(),
        discriminator=data.orphan_batch,
    )
