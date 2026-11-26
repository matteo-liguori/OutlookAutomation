"""ContactsPort over the Thunderbird bridge: explicitly bound address books only.

Contract: docs/contracts/08-ports.md. The connector searches the address books
named in its binding and nothing else; it does not reach a university or
global directory, and it reports an incomplete scan as a gap (P-4).
"""

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import Field, model_validator

from chamberlain.connectors.calendar_support.paging import bad_cursor, decode_token, encode_token
from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.client import BridgeClient, BridgeFailure, typed
from chamberlain.connectors.thunderbird.identity import (
    decode_contact_locator,
    encode_contact_locator,
)
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.capabilities import (
    Capability,
    CapabilityName,
    CapabilitySet,
    HealthState,
    SourceHealth,
    VerificationLevel,
)
from chamberlain.domain.contacts import Contact, ContactQuery
from chamberlain.ports.clock import Clock
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode


class ContactsBinding(Model):
    kind: Literal["thunderbird_contacts"] = "thunderbird_contacts"
    address_book_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.address_book_ids)) != len(self.address_book_ids):
            raise ValueError("address_book_ids must be unique")
        return self


def _contact(source: str, found: protocol.ContactInfo) -> Contact:
    addresses = tuple(e.strip().lower() for e in found.emails if e.strip())
    fields: dict[str, object] = {
        "source": source,
        "locator": encode_contact_locator(found.book_id, found.id),
        "name": found.name,
    }
    try:
        return Contact.model_validate({**fields, "emails": addresses})
    except ValueError:
        return Contact.model_validate({**fields, "emails": ()})


class ThunderbirdContacts:
    """Implements `chamberlain.ports.ContactsPort`."""

    def __init__(
        self, client: BridgeClient, bindings: Mapping[str, ContactsBinding], clock: Clock
    ) -> None:
        self._client = client
        self._bindings = dict(bindings)
        self._clock = clock

    def _binding(self, source: str) -> ContactsBinding:
        try:
            return self._bindings[source]
        except KeyError:
            raise ChamberlainError(
                ErrorCode.SOURCE_NOT_CONFIGURED, "That contacts source is not bound."
            ) from None

    def capabilities(self, source: str) -> CapabilitySet:
        self._binding(source)
        return CapabilitySet(
            source=source,
            capabilities=(
                Capability(
                    name=CapabilityName.CONTACTS_SEARCH,
                    level=VerificationLevel.IMPLEMENTED,
                    limits={"page_size": 50},
                ),
            ),
        )

    def health(self, source: str) -> SourceHealth:
        self._binding(source)
        checked = self._clock.now()
        try:
            self._client.call(protocol.Command.STATUS, protocol.StatusArgs())
        except BridgeFailure:
            state = HealthState.UNAVAILABLE
        else:
            state = HealthState.OK
        return SourceHealth.model_validate(
            {"source": source, "state": state, "checked_at": checked, "fresh_as_of": None}
        )

    def read(self, source: str, locator: str) -> Contact:
        """The one contact a locator names, read live from its bound address book."""
        binding = self._binding(source)
        book_id, contact_id = decode_contact_locator(locator)
        if book_id not in binding.address_book_ids:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE, "That contact is not in a bound address book."
            )
        reply = self._client.call(
            protocol.Command.CONTACT_READ,
            protocol.ContactReadArgs(address_book_id=book_id, contact_id=contact_id),
        )
        return _contact(source, typed(reply, protocol.ContactReadResult).contact)

    def search(self, source: str, query: ContactQuery, cursor: str | None = None) -> Page[Contact]:
        binding = self._binding(source)
        tag = sha256_text(query.text)[:12]
        skip = 0
        if cursor is not None:
            bound, _, offset = decode_token(cursor, "c1").partition(":")
            if bound != tag or not offset.isdigit():
                raise bad_cursor()
            skip = int(offset)
        reply = self._client.call(
            protocol.Command.CONTACTS,
            protocol.ContactsArgs(
                address_book_ids=binding.address_book_ids,
                text=query.text,
                limit=query.limit,
                skip=skip,
            ),
        )
        result = typed(reply, protocol.ContactsResult)
        items = [_contact(source, found) for found in result.contacts]
        next_cursor = None
        if result.more:
            next_cursor = encode_token(f"{tag}:{skip + query.limit}", "c1")
        gaps: tuple[Gap, ...] = ()
        if not result.complete:
            gaps = (
                Gap.model_validate(
                    {"source": source, "reason": "An address book was only partly scanned."}
                ),
            )
        return Page[Contact](
            items=tuple(items),
            cursor=next_cursor,
            complete=next_cursor is None and not gaps,
            gaps=gaps,
        )
