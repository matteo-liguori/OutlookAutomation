"""Detectors for rows other tracks keep: new mail, grants, unsettled writes, storage.

Contract: docs/contracts/07-attention.md (A-4, A-5, A-8). Summaries name handles only (E-8).
The watermark scan (`attention/watermark.py`) records each new message once, whatever folder
holds it; `new_unmatched` lists what is waiting as a count. Mail the owner sent that matches an
open card is a separate low-priority line, never a triage item by itself.
"""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft, draft
from chamberlain.attention.tuning import MAIL_SENT_PRIORITY
from chamberlain.domain.events import EventKind
from chamberlain.domain.grants import Requirement
from chamberlain.domain.intents import IntentKind
from chamberlain.limits import SUMMARY_MAX


def new_unmatched(data: DetectorInput) -> Iterable[EventDraft]:
    for batch in data.mail:
        if batch.sent:
            yield draft(
                EventKind.MAIL_NEW_UNMATCHED,
                batch.source,
                f"You sent {batch.count} mail from another device in {batch.source} that "
                "matches an open card.",
                EventAttrs(source=batch.source, since=batch.oldest_received, reason="sent"),
                priority=MAIL_SENT_PRIORITY,
                discriminator=f"sent:{batch.first_seq}",
            )
            continue
        extra = f", {batch.suggested} with a suggested card" if batch.suggested else ""
        yield draft(
            EventKind.MAIL_NEW_UNMATCHED,
            batch.source,
            f"{batch.count} new mail in {batch.source}{extra}.",
            EventAttrs(source=batch.source, since=batch.oldest_received, reason="received"),
            discriminator=str(batch.first_seq),
        )


def grant_needed(data: DetectorInput) -> Iterable[EventDraft]:
    for grant in data.grants:
        yield draft(
            EventKind.GRANT_NEEDED,
            grant.handle,
            f"Grant {grant.handle} waits for the user to confirm it.",
            EventAttrs(requirement=Requirement.HUMAN_CONFIRMATION, card=grant.card),
        )


def grant_review_due(data: DetectorInput) -> Iterable[EventDraft]:
    for grant in data.review_grants:
        yield draft(
            EventKind.GRANT_REVIEW_DUE,
            grant.handle,
            f"Grant {grant.handle} is due for the user to review.",
            EventAttrs(reason=grant.state, card=grant.card),
        )


def operation_uncertain(data: DetectorInput) -> Iterable[EventDraft]:
    """Writes still unsettled after the harness's own 24 hours (W-13, W-37, A-4)."""
    for operation in data.operations:
        try:
            kind = IntentKind(operation.kind)
        except ValueError:
            kind = None
        yield draft(
            EventKind.OPERATION_UNCERTAIN,
            operation.handle,
            f"{operation.kind} {operation.handle} is still unsettled after a day; "
            "the harness could not find it.",
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
        yield draft(
            EventKind.SEND_MISSING,
            send.handle,
            f'{head}"{subject}" {tail}'[:SUMMARY_MAX],
            EventAttrs(
                intent_kind=IntentKind.MAIL_SEND,
                reason="outbox" if stuck else "missing",
                card=send.card,
                source=send.source,
            ),
            discriminator=send.result,
        )


def over_budget(data: DetectorInput) -> Iterable[EventDraft]:
    """Storage budgets, as memory records them (M-26)."""
    for budget in data.budgets:
        if budget.scope is None:
            summary = "Stored files have passed the overall budget."
        else:
            summary = f"Card {budget.scope} holds more stored files than its budget."
        yield draft(
            EventKind.STORAGE_OVER_BUDGET,
            budget.scope,
            summary,
            EventAttrs(card=budget.scope, reason="card" if budget.scope else "total"),
        )


def orphans(data: DetectorInput) -> Iterable[EventDraft]:
    """One event per scan that found orphans; a later scan is a new condition (A-3, M-27)."""
    if data.orphan_batch is None or data.orphan_count == 0:
        return
    yield draft(
        EventKind.STORAGE_ORPHANS,
        None,
        "Unknown files were set aside and will be deleted after a grace period.",
        discriminator=data.orphan_batch,
    )
