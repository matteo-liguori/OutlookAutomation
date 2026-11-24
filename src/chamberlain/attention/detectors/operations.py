"""Writes that may have happened and are not settled (W-9, W-13, A-4)."""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.events import EventKind
from chamberlain.domain.intents import IntentKind


def operation_uncertain(data: DetectorInput) -> Iterable[EventDraft]:
    for operation in data.operations:
        try:
            kind = IntentKind(operation.kind)
        except ValueError:
            kind = None
        yield EventDraft(
            EventKind.OPERATION_UNCERTAIN,
            operation.handle,
            f"Operation {operation.handle} may have happened and is not settled.",
            PRIORITY[EventKind.OPERATION_UNCERTAIN],
            EventAttrs(intent_kind=kind),
        )
