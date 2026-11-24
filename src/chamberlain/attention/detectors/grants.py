"""Grants that wait for the user, and standing grants due for review (G-8, G-12)."""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.events import EventKind
from chamberlain.domain.grants import Requirement


def grant_needed(data: DetectorInput) -> Iterable[EventDraft]:
    for grant in data.grants:
        yield EventDraft(
            EventKind.GRANT_NEEDED,
            grant.handle,
            f"Grant {grant.handle} waits for the user to confirm it.",
            PRIORITY[EventKind.GRANT_NEEDED],
            EventAttrs(requirement=Requirement.HUMAN_CONFIRMATION, card=grant.card),
        )


def grant_review_due(data: DetectorInput) -> Iterable[EventDraft]:
    for grant in data.review_grants:
        yield EventDraft(
            EventKind.GRANT_REVIEW_DUE,
            grant.handle,
            f"Grant {grant.handle} is due for the user to review.",
            PRIORITY[EventKind.GRANT_REVIEW_DUE],
            EventAttrs(reason=grant.state, card=grant.card),
        )
