"""Which open card a new message probably belongs to. A suggestion, never a link.

The agent decides every link itself (owner ruling); this only makes the suggestion
accurate: an exact reply-header match first, then a sender who corresponds with
exactly one open card. A sender who fits several cards suggests nothing.
"""

import json
import logging

from chamberlain.app.context import Context
from chamberlain.attention.tuning import MAIL_CORRESPONDENT_LOOKUPS, MAIL_HEADER_READS
from chamberlain.domain.mail import MessageSummary, folded_message_id
from chamberlain.memory import repo as memory_repo
from chamberlain.ports.mail import MailPort
from chamberlain.schema.safelog import log_failure
from chamberlain.writes import repo as writes_repo

log = logging.getLogger("chamberlain.attention")


class Suggester:
    """Built once per tick; cheap until a new message arrives."""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._by_handle = memory_repo.card_links(ctx.db)
        self._by_id = {
            folded_message_id(key): card for key, card in self._by_handle.items() if "@" in key
        }
        self._reads: dict[str, int] = {}
        self._people: dict[str, dict[str, set[str]]] = {}

    def suggest(
        self, port: MailPort, source: str, handle: str | None, summary: MessageSummary
    ) -> str | None:
        found = self._by_handle.get(handle) if handle is not None else None
        if found is not None:
            return found
        card = self._by_reply_headers(port, source, summary)
        if card is not None:
            return card
        return self._by_correspondent(port, source, summary.sender.address)

    def suggest_sent(
        self, port: MailPort, source: str, handle: str | None, summary: MessageSummary
    ) -> str | None:
        """The card a message the owner sent probably belongs to: the thread it answers, else
        the one card its recipients all point to."""
        found = self._by_handle.get(handle) if handle is not None else None
        if found is not None:
            return found
        card = self._by_reply_headers(port, source, summary)
        if card is not None:
            return card
        people = self._correspondents(port, source)
        cards: set[str] = set()
        for person in (*summary.to, *summary.cc):
            cards |= people.get(person.address.casefold(), set())
        return next(iter(cards)) if len(cards) == 1 else None

    def _by_reply_headers(self, port: MailPort, source: str, summary: MessageSummary) -> str | None:
        if not self._by_id:
            return None
        in_reply_to = summary.in_reply_to
        references = summary.references
        if not in_reply_to and not references and self._reads.get(source, 0) < MAIL_HEADER_READS:
            self._reads[source] = self._reads.get(source, 0) + 1
            try:
                full = port.read(source, summary.locator)
                in_reply_to, references = full.in_reply_to, full.references
            except Exception:
                log_failure(log, "reading reply headers failed for %s", source)
        for message_id in (in_reply_to, *reversed(references)):
            card = self._by_id.get(folded_message_id(message_id)) if message_id else None
            if card is not None:
                return card
        return None

    def _correspondents(self, port: MailPort, source: str) -> dict[str, set[str]]:
        if source in self._people:
            return self._people[source]
        people: dict[str, set[str]] = {}
        for card, intent in writes_repo.sent_recipients(self._ctx.db):
            try:
                data: dict[str, object] = json.loads(intent)
            except ValueError:
                continue
            for field in ("to", "cc"):
                listed = data.get(field)
                addresses: list[object] = list(listed) if isinstance(listed, list) else []  # pyright: ignore[reportUnknownArgumentType]
                for address in addresses:
                    people.setdefault(str(address).casefold(), set()).add(card)
        looked = 0
        for card, linked_source, locator in memory_repo.linked_message_refs(self._ctx.db):
            if linked_source != source or looked >= MAIL_CORRESPONDENT_LOOKUPS:
                continue
            looked += 1
            try:
                sender = port.resolve(source, locator).sender.address
            except Exception:
                continue
            people.setdefault(sender.casefold(), set()).add(card)
        self._people[source] = people
        return people

    def _by_correspondent(self, port: MailPort, source: str, address: str) -> str | None:
        cards = self._correspondents(port, source).get(address.casefold(), set())
        return next(iter(cards)) if len(cards) == 1 else None
