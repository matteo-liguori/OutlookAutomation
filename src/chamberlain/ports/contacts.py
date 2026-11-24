"""ContactsPort: read-only address book search.

Contract: docs/contracts/08-ports.md. The executable form is
`chamberlain.testing.contacts_contract.ContactsPortContract`.
"""

from typing import Protocol, runtime_checkable

from chamberlain.domain.capabilities import CapabilitySet, SourceHealth
from chamberlain.domain.contacts import Contact, ContactQuery
from chamberlain.ports.page import Page


@runtime_checkable
class ContactsPort(Protocol):
    def capabilities(self, source: str) -> CapabilitySet: ...

    def health(self, source: str) -> SourceHealth: ...

    def search(self, source: str, query: ContactQuery, cursor: str | None = None) -> Page[Contact]:
        """Contacts whose name or address contains the text (P-3, P-4)."""
        ...

    def read(self, source: str, locator: str) -> Contact:
        """The one contact a locator from `search` names. An unknown locator raises
        `request.unknown_handle`."""
        ...
