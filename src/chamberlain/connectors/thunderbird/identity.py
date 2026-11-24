"""Stable locators, Message-ID forms and the text normalisers shared with the add-on.

Contract: docs/contracts/12-bridge-v2.md section 11. A locator survives a
Thunderbird restart because it names the account, folder and Message-ID,
never a session integer (P-6).
"""

import base64
import json
from dataclasses import dataclass
from email.utils import parseaddr
from typing import cast

from chamberlain.domain.mail import Address
from chamberlain.schema.errors import ChamberlainError, ErrorCode

LOCATOR_PREFIX = "tb:"
CONTACT_PREFIX = "tbc:"
_ASCII_SPACE = " \t\n"


@dataclass(frozen=True)
class MessageLocator:
    account_id: str
    folder_id: str
    message_id: str
    """Bare Message-ID, without angle brackets."""


def bare(message_id: str) -> str:
    """The Message-ID as the add-on uses it: no angle brackets."""
    text = message_id.strip()
    if text.startswith("<") and text.endswith(">"):
        return text[1:-1]
    return text


def bracketed(message_id: str) -> str:
    """The Message-ID as it appears in a header, which is what leaves the connector."""
    return f"<{bare(message_id)}>"


def encode_locator(account_id: str, folder_id: str, message_id: str) -> str:
    raw = json.dumps([account_id, folder_id, bare(message_id)], separators=(",", ":"))
    return LOCATOR_PREFIX + base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip(
        "="
    )


def _unknown(detail: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.REQUEST_UNKNOWN_HANDLE, "That message locator is not recognised.", detail=detail
    )


def decode_locator(locator: str, *, account_id: str) -> MessageLocator:
    """Decode a locator; anything malformed or foreign is an unknown handle, never a guess."""
    if not locator.startswith(LOCATOR_PREFIX) or len(locator) > 12_000:
        raise _unknown("not a Thunderbird locator")
    token = locator[len(LOCATOR_PREFIX) :]
    try:
        parts: object = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, UnicodeError):
        raise _unknown("undecodable") from None
    items = cast(list[object], parts) if isinstance(parts, list) else []
    if len(items) != 3 or not all(isinstance(part, str) and part for part in items):
        raise _unknown("wrong shape")
    found_account, folder_id, message_id = (str(part) for part in items)
    if found_account != account_id:
        raise _unknown("another account")
    return MessageLocator(found_account, folder_id, message_id)


def encode_contact_locator(book_id: str, contact_id: str) -> str:
    raw = json.dumps([book_id, contact_id], separators=(",", ":"))
    return CONTACT_PREFIX + base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip(
        "="
    )


def decode_contact_locator(locator: str) -> tuple[str, str]:
    """The address book and contact ids a contact locator names."""
    if not locator.startswith(CONTACT_PREFIX):
        raise _unknown_contact("not a Thunderbird contact locator")
    token = locator[len(CONTACT_PREFIX) :]
    try:
        parts: object = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, UnicodeError):
        raise _unknown_contact("undecodable") from None
    if not isinstance(parts, list):
        raise _unknown_contact("wrong shape")
    items = cast(list[object], parts)
    if len(items) != 2 or not all(isinstance(item, str) for item in items):
        raise _unknown_contact("wrong shape")
    return cast(str, items[0]), cast(str, items[1])


def _unknown_contact(detail: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.REQUEST_UNKNOWN_HANDLE, "That contact locator is not recognised.", detail=detail
    )


def normalise_body(text: str) -> str:
    """CRLF to LF, ASCII whitespace trimmed; identical to `normaliseBody` in the add-on."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip(_ASCII_SPACE)


def parse_address(value: str) -> Address:
    """`Name <a@b.c>` to an `Address`; an unparseable author becomes an inert placeholder."""
    name, address = parseaddr(value)
    try:
        return Address.model_validate({"address": address.strip().lower(), "name": name})
    except ValueError:
        return Address.model_validate({"address": "unknown@unknown.invalid", "name": value[:200]})


def emails_only(values: tuple[str, ...]) -> tuple[Address, ...]:
    return tuple(parse_address(value) for value in values)
