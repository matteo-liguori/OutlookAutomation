"""New mail, one triage item per source.

The watermark scan (`attention/watermark.py`) records each new message once, whatever folder
holds it; this lists what is waiting as a count. Mail the owner sent that matches an open card
is a separate low-priority line, never a triage item by itself. The agent lists the mail with
`find` (sender and subject lines, each with a suggested card when there is one) and decides every
link itself, so `mail.new_for_card` is retired as a trigger: its suggestions ride inside the batch.
"""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import MAIL_SENT_PRIORITY, PRIORITY
from chamberlain.domain.events import EventKind


def new_for_card(data: DetectorInput) -> Iterable[EventDraft]:
    return ()


def new_unmatched(data: DetectorInput) -> Iterable[EventDraft]:
    for batch in data.mail:
        if batch.sent:
            yield EventDraft(
                EventKind.MAIL_NEW_UNMATCHED,
                batch.source,
                f"You sent {batch.count} mail from another device in {batch.source} that "
                "matches an open card.",
                MAIL_SENT_PRIORITY,
                EventAttrs(source=batch.source, since=batch.oldest_received, reason="sent"),
                discriminator=f"sent:{batch.first_seq}",
            )
            continue
        extra = f", {batch.suggested} with a suggested card" if batch.suggested else ""
        yield EventDraft(
            EventKind.MAIL_NEW_UNMATCHED,
            batch.source,
            f"{batch.count} new mail in {batch.source}{extra}.",
            PRIORITY[EventKind.MAIL_NEW_UNMATCHED],
            EventAttrs(source=batch.source, since=batch.oldest_received, reason="received"),
            discriminator=str(batch.first_seq),
        )
