"""New mail, one triage item per source.

The watermark scan (`attention/watermark.py`) records each new message once; this lists
what is waiting as a count. The agent lists the mail with `find` (sender and subject
lines, each with a suggested card when there is one) and decides every link itself, so
`mail.new_for_card` is retired as a trigger: its suggestions ride inside the batch.
"""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.events import EventKind


def new_for_card(data: DetectorInput) -> Iterable[EventDraft]:
    return ()


def new_unmatched(data: DetectorInput) -> Iterable[EventDraft]:
    for batch in data.mail:
        extra = f", {batch.suggested} with a suggested card" if batch.suggested else ""
        yield EventDraft(
            EventKind.MAIL_NEW_UNMATCHED,
            batch.source,
            f"{batch.count} new mail in {batch.source}{extra}.",
            PRIORITY[EventKind.MAIL_NEW_UNMATCHED],
            EventAttrs(source=batch.source, since=batch.oldest_received),
            discriminator=str(batch.first_seq),
        )
