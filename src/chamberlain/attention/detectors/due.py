"""Due work: actions, snoozed actions coming back, cards due for review.

Contract: docs/contracts/07-attention.md. Summaries name handles only (E-8).
"""

from collections.abc import Iterable
from datetime import datetime

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.events import EventKind


def _at(stamp: str | None) -> datetime | None:
    return None if stamp is None else datetime.fromisoformat(stamp)


def action_due(data: DetectorInput) -> Iterable[EventDraft]:
    """An open action past its due time that is not snoozed (A-2: snoozed is silent)."""
    for action in data.actions:
        due, snooze = _at(action.due_at), _at(action.snooze_until)
        if due is None or due > data.now or (snooze is not None and snooze > data.now):
            continue
        yield EventDraft(
            EventKind.ACTION_DUE,
            action.handle,
            f"Action {action.handle} on card {action.card} is due.",
            PRIORITY[EventKind.ACTION_DUE],
            EventAttrs(card=action.card),
        )


def reminder_due(data: DetectorInput) -> Iterable[EventDraft]:
    """A snoozed action whose snooze has ended and which is not yet due."""
    for action in data.actions:
        due, snooze = _at(action.due_at), _at(action.snooze_until)
        if snooze is None or snooze > data.now or (due is not None and due <= data.now):
            continue
        yield EventDraft(
            EventKind.REMINDER_DUE,
            action.handle,
            f"Action {action.handle} on card {action.card} is back from its snooze.",
            PRIORITY[EventKind.REMINDER_DUE],
            EventAttrs(card=action.card),
        )


def card_review_due(data: DetectorInput) -> Iterable[EventDraft]:
    yield from _complex_decided(data)
    for card in data.cards:
        if card.over_cap:
            yield EventDraft(
                EventKind.CARD_REVIEW_DUE,
                card.handle,
                f"Card {card.handle} is over its size cap and needs trimming.",
                PRIORITY[EventKind.CARD_REVIEW_DUE] + 5,
                EventAttrs(card=card.handle, reason="over_cap"),
            )
            continue
        review = _at(card.review_at)
        if review is not None and review <= data.now:
            yield EventDraft(
                EventKind.CARD_REVIEW_DUE,
                card.handle,
                f"Card {card.handle} is due for review.",
                PRIORITY[EventKind.CARD_REVIEW_DUE],
                EventAttrs(card=card.handle, reason="review"),
            )


def _complex_decided(data: DetectorInput) -> Iterable[EventDraft]:
    """Tell the agent how the owner answered a request for a larger card limit (M-36)."""
    for decision in data.complex_decisions:
        if decision.state == "approved":
            summary = f"The owner approved a larger limit for card {decision.card}."
            reason = "complex_approved"
        else:
            until = (decision.blocked_until or "")[:10]
            summary = (
                f"The owner declined a larger limit for card {decision.card}; "
                f"ask again after {until}."
            )
            reason = "complex_declined"
        yield EventDraft(
            EventKind.CARD_REVIEW_DUE,
            decision.card,
            summary,
            PRIORITY[EventKind.CARD_REVIEW_DUE] + 3,
            EventAttrs(card=decision.card, reason=reason),
            discriminator=f"{reason}:{decision.decided_at}",
        )
