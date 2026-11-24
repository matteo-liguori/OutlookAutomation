"""New unread mail, split by whether a card already covers it.

Interim reading (board QUESTION): `MessageSummary` carries no reply headers, so a
message matches a card when its handle or Message-ID is already linked, or when
its normalised subject contains, or is contained in, an open card title.
"""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft, NewMail
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.events import EventKind

_MIN_MATCH = 6


def _card_for(data: DetectorInput, mail: NewMail) -> str | None:
    linked = data.linked_cards.get(mail.handle) or data.linked_cards.get(mail.internet_message_id)
    if linked is not None:
        return linked
    subject = mail.subject_norm
    if len(subject) < _MIN_MATCH:
        return None
    for card in sorted(data.open_cards, key=lambda c: c.handle):
        title = card.title.casefold().strip()
        if len(title) >= _MIN_MATCH and (title in subject or subject in title):
            return card.handle
    return None


def _split(data: DetectorInput) -> tuple[list[tuple[NewMail, str]], list[NewMail]]:
    matched: list[tuple[NewMail, str]] = []
    unmatched: list[NewMail] = []
    for delta in data.mail:
        for mail in delta.items:
            card = _card_for(data, mail)
            if card is None:
                unmatched.append(mail)
            else:
                matched.append((mail, card))
    return matched, unmatched


def new_for_card(data: DetectorInput) -> Iterable[EventDraft]:
    for mail, card in _split(data)[0]:
        yield EventDraft(
            EventKind.MAIL_NEW_FOR_CARD,
            mail.handle,
            f"New unread mail {mail.handle} may belong to card {card}.",
            PRIORITY[EventKind.MAIL_NEW_FOR_CARD],
            EventAttrs(source=mail.source, card=card),
        )


def new_unmatched(data: DetectorInput) -> Iterable[EventDraft]:
    for mail in _split(data)[1]:
        yield EventDraft(
            EventKind.MAIL_NEW_UNMATCHED,
            mail.handle,
            f"New unread mail {mail.handle} matches no card.",
            PRIORITY[EventKind.MAIL_NEW_UNMATCHED],
            EventAttrs(source=mail.source),
        )
