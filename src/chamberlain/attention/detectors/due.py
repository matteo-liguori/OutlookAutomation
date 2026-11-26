"""Due work: actions, snoozed actions coming back, cards due for review.

Contract: docs/contracts/07-attention.md. Summaries name handles only (E-8).
"""

from collections.abc import Callable, Iterable
from datetime import datetime

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft, draft
from chamberlain.domain.events import EventKind


def _at(stamp: str | None) -> datetime | None:
    return None if stamp is None else datetime.fromisoformat(stamp)


def _actions(
    data: DetectorInput,
    kind: EventKind,
    tail: str,
    wanted: Callable[[datetime | None, datetime | None, datetime], bool],
) -> Iterable[EventDraft]:
    for action in data.actions:
        if wanted(_at(action.due_at), _at(action.snooze_until), data.now):
            yield draft(
                kind,
                action.handle,
                f"Action {action.handle} on card {action.card} {tail}",
                EventAttrs(card=action.card),
            )


def action_due(data: DetectorInput) -> Iterable[EventDraft]:
    """An open action past its due time that is not snoozed (A-2: snoozed is silent)."""
    return _actions(
        data,
        EventKind.ACTION_DUE,
        "is due.",
        lambda due, snooze, now: (
            due is not None and due <= now and (snooze is None or snooze <= now)
        ),
    )


def reminder_due(data: DetectorInput) -> Iterable[EventDraft]:
    """A snoozed action whose snooze has ended and which is not yet due."""
    return _actions(
        data,
        EventKind.REMINDER_DUE,
        "is back from its snooze.",
        lambda due, snooze, now: (
            snooze is not None and snooze <= now and (due is None or due > now)
        ),
    )


def card_review_due(data: DetectorInput) -> Iterable[EventDraft]:
    yield from _complex_decided(data)
    for card in data.cards:
        if card.over_cap:
            yield draft(
                EventKind.CARD_REVIEW_DUE,
                card.handle,
                f"Card {card.handle} is over its size cap and needs trimming.",
                EventAttrs(card=card.handle, reason="over_cap"),
                bump=5,
            )
            continue
        review = _at(card.review_at)
        if review is not None and review <= data.now:
            # A card holding notes from the old system is not judged for closing: the agent
            # first folds the notes into it (rule card.context, shown when it opens the card).
            yield draft(
                EventKind.CARD_REVIEW_DUE,
                card.handle,
                f"Card {card.handle} is due for review and still holds notes from the old system."
                if card.has_context
                else f"Card {card.handle} is due for review.",
                EventAttrs(card=card.handle, reason="context" if card.has_context else "review"),
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
        yield draft(
            EventKind.CARD_REVIEW_DUE,
            decision.card,
            summary,
            EventAttrs(card=decision.card, reason=reason),
            bump=3,
            discriminator=f"{reason}:{decision.decided_at}",
        )
