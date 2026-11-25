"""Writes still unsettled after the harness's own 24 hours (W-13, W-37, A-4)."""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.events import EventKind
from chamberlain.domain.intents import IntentKind
from chamberlain.limits import SUMMARY_MAX


def operation_uncertain(data: DetectorInput) -> Iterable[EventDraft]:
    for operation in data.operations:
        try:
            kind = IntentKind(operation.kind)
        except ValueError:
            kind = None
        yield EventDraft(
            EventKind.OPERATION_UNCERTAIN,
            operation.handle,
            f"{operation.kind} {operation.handle} is still unsettled after a day; "
            "the harness could not find it.",
            PRIORITY[EventKind.OPERATION_UNCERTAIN],
            EventAttrs(intent_kind=kind),
        )


def send_missing(data: DetectorInput) -> Iterable[EventDraft]:
    """One event per uncertain send the harness's 24-hour look could not find (W-37).

    `reason` is `missing` (nowhere it should be) or `outbox` (stuck in the mail client).
    A change of answer is a new event; the old one closes with the condition.
    """
    for send in data.sends:
        hours = max(0, int((data.now - send.dispatched_at).total_seconds() // 3600))
        who = send.recipients[0] if send.recipients else "?"
        more = f" +{len(send.recipients) - 1}" if len(send.recipients) > 1 else ""
        stuck = send.result == "queued"
        tail = (
            f"is stuck in the Outbox after {hours} h."
            if stuck
            else f"is missing after {hours} h (not in Sent, Outbox, Drafts)."
        )
        head = f"Send to {who}{more} "
        room = SUMMARY_MAX - len(head) - len(tail) - len(' "" ')
        subject = send.subject
        if len(subject) > room:
            subject = subject[: max(room - 1, 0)] + "."
        yield EventDraft(
            EventKind.SEND_MISSING,
            send.handle,
            f'{head}"{subject}" {tail}'[:SUMMARY_MAX],
            PRIORITY[EventKind.SEND_MISSING],
            EventAttrs(
                intent_kind=IntentKind.MAIL_SEND,
                reason="outbox" if stuck else "missing",
                card=send.card,
                source=send.source,
            ),
            discriminator=send.result,
        )
