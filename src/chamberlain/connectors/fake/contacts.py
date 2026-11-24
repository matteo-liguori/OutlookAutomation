"""In-memory ContactsPort.

Contract: docs/contracts/08-ports.md.
"""

from chamberlain.connectors.fake.world import FakeWorld, Fault, StoredContact
from chamberlain.domain.capabilities import (
    Capability,
    CapabilityName,
    CapabilitySet,
    SourceHealth,
    VerificationLevel,
)
from chamberlain.domain.contacts import Contact, ContactQuery
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode


class FakeContacts:
    """Implements `chamberlain.ports.ContactsPort` over a `FakeWorld`."""

    def __init__(self, world: FakeWorld) -> None:
        self._world = world

    def add_source(self, source: str) -> None:
        self._world.contacts.setdefault(source, [])

    def add_contact(self, source: str, name: str, *emails: str) -> str:
        """Store a contact and return its locator."""
        contact_id = f"c{len(self._world.contacts[source]) + 1:04d}"
        self._world.contacts[source].append(StoredContact(contact_id, name, tuple(emails)))
        return contact_id

    def capabilities(self, source: str) -> CapabilitySet:
        self._world.require_source(self._world.contacts, source)
        capability = Capability(
            name=CapabilityName.CONTACTS_SEARCH, level=VerificationLevel.IMPLEMENTED
        )
        return CapabilitySet.model_validate({"source": source, "capabilities": [capability]})

    def health(self, source: str) -> SourceHealth:
        self._world.require_source(self._world.contacts, source)
        return self._world.health(source)

    def read(self, source: str, locator: str) -> Contact:
        self._world.require_source(self._world.contacts, source)
        self._world.begin_read(source)
        for c in self._world.contacts[source]:
            if c.id == locator:
                return Contact.model_validate(
                    {"source": source, "locator": c.id, "name": c.name, "emails": c.emails}
                )
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such contact.")

    def search(self, source: str, query: ContactQuery, cursor: str | None = None) -> Page[Contact]:
        self._world.require_source(self._world.contacts, source)
        self._world.begin_read(source)
        try:
            offset = int(cursor or "0")
        except ValueError:
            raise ChamberlainError(
                ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid."
            ) from None
        needle = query.text.casefold()
        matches = [
            c
            for c in self._world.contacts[source]
            if needle in c.name.casefold() or any(needle in e.casefold() for e in c.emails)
        ]
        window = matches[offset : offset + query.limit]
        more = offset + query.limit < len(matches)
        gaps: tuple[Gap, ...] = ()
        if self._world.take(Fault.STALE):
            gaps = (Gap.model_validate({"source": source, "reason": "The address book is stale."}),)
        items = tuple(
            Contact.model_validate(
                {"source": source, "locator": c.id, "name": c.name, "emails": c.emails}
            )
            for c in window
        )
        return Page[Contact](
            items=items,
            cursor=str(offset + query.limit) if more else None,
            complete=not more and not gaps,
            gaps=gaps,
        )
