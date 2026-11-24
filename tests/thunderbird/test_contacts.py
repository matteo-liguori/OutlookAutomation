"""Reading one contact by locator through the real add-on (contact_read)."""

import pytest
from thunderbird_rig import Stack

from chamberlain.connectors.thunderbird.identity import encode_contact_locator
from chamberlain.schema.errors import ChamberlainError, ErrorCode


def test_a_contact_is_read_by_its_locator(stack: Stack) -> None:
    locator = stack.seed_contact("Ada Reader", "Ada@Example.org")
    contact = stack.contacts.read("contacts:test", locator)
    assert (contact.name, contact.emails, contact.locator) == (
        "Ada Reader",
        ("ada@example.org",),
        locator,
    )


@pytest.mark.parametrize(
    "locator", ["tb:nope", "tbc:!!", encode_contact_locator("book1", "missing")]
)
def test_an_unknown_contact_is_an_unknown_handle(stack: Stack, locator: str) -> None:
    with pytest.raises(ChamberlainError) as raised:
        stack.contacts.read("contacts:test", locator)
    assert raised.value.code is ErrorCode.REQUEST_UNKNOWN_HANDLE


def test_a_contact_outside_the_bound_books_is_refused_without_a_call(stack: Stack) -> None:
    with pytest.raises(ChamberlainError) as raised:
        stack.contacts.read("contacts:test", encode_contact_locator("book2", "c1"))
    assert raised.value.code is ErrorCode.REQUEST_UNKNOWN_HANDLE
