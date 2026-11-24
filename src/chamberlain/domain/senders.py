"""Authorised senders: which incoming email may count as an instruction (owner decision 2026-11-24).

An email is an instruction ONLY when its From address is listed in the private profile AND the
receiving provider's own authentication result passed for that same domain. Everything else in
email stays untrusted data. Pure: no clock, no store, no connector.

`Authentication-Results` is only evidence in its topmost copy: the receiving provider adds it
above any header the sender wrote, so a forged `dkim=pass` further down is never read.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from typing import Final, Self

from pydantic import Field, field_validator, model_validator

from chamberlain.domain.intents import IntentKind
from chamberlain.schema.base import EmailAddress, Model

ASK_ONLY: Final = "ask_only"
NO_AUTHSERV_ID: Final = "(none)"
"""Listed in `trusted_authserv_ids` to accept a header that starts with a result (Exchange)."""
DEFAULT_AUTHSERV_IDS: Final[tuple[str, ...]] = (
    "mx.google.com",
    "*.google.com",
    "*.outlook.com",
    "*.protection.outlook.com",
    "mx.icloud.com",
)
INSTRUCTION_MAX_AGE: Final = timedelta(days=7)
"""An older email is not fresh enough to authorise anything now."""
REF_PREFIX: Final = "instruction"
_KINDS: Final = frozenset(kind.value for kind in IntentKind)
_RESULT = re.compile(r"^\s*([a-z0-9-]+)\s*=\s*([a-z]+)\b(.*)$", re.IGNORECASE | re.DOTALL)
_PROPERTY = re.compile(r"\b([a-z]+\.[a-z-]+)\s*=\s*[\"']?([^\s;\"'()]+)", re.IGNORECASE)


class OtherSender(Model):
    address: EmailAddress
    scope: tuple[str, ...] = Field(min_length=1)
    """Intent kinds this sender may instruct, or `ask_only` (may ask questions, nothing else)."""
    note: str = Field(default="", max_length=120)

    @field_validator("scope")
    @classmethod
    def _known_scope(cls, scope: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [item for item in scope if item != ASK_ONLY and item not in _KINDS]
        if unknown:
            raise ValueError(f"unknown scope {unknown[0]!r}: use an intent kind or {ASK_ONLY!r}")
        if ASK_ONLY in scope and len(scope) > 1:
            raise ValueError(f"{ASK_ONLY!r} cannot be combined with intent kinds")
        return scope


class AuthorisedSenders(Model):
    owner: tuple[EmailAddress, ...] = ()
    """The owner's own addresses. A verified email from one counts like chat."""
    others: tuple[OtherSender, ...] = ()
    trusted_authserv_ids: tuple[str, ...] = DEFAULT_AUTHSERV_IDS
    """Which receiving servers' `Authentication-Results` to believe. Fail closed."""

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        listed = [*self.owner, *(o.address for o in self.others)]
        if len(set(listed)) != len(listed):
            raise ValueError("an address is listed once, as owner or as one other sender")
        return self


# --- the provider's authentication result ----------------------------------------------


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower()


def aligned(signing: str, from_domain: str) -> bool:
    """The signing domain is the From domain, or a parent or child of it (relaxed alignment)."""
    signing, from_domain = signing.strip(".").lower(), from_domain.strip(".").lower()
    if "." not in signing or "." not in from_domain:
        return False
    return (
        signing == from_domain
        or from_domain.endswith("." + signing)
        or signing.endswith("." + from_domain)
    )


@dataclass(frozen=True)
class AuthVerdict:
    passed: bool
    reason: str
    """Short and safe to show: it never repeats header text."""


def _server_trusted(identifier: str, trusted: tuple[str, ...]) -> bool:
    if not identifier:
        return NO_AUTHSERV_ID in trusted
    return any(fnmatchcase(identifier.lower(), pattern.lower()) for pattern in trusted)


def check_authentication(
    headers: tuple[str, ...], from_address: str, trusted_authserv_ids: tuple[str, ...]
) -> AuthVerdict:
    """Did the receiving provider verify `from_address`'s domain? Only the topmost header counts."""
    if not headers:
        return AuthVerdict(False, "The message carries no authentication result.")
    parts = [part.strip() for part in headers[0].split(";")]
    identifier = ""
    first = parts[0].split(" ")[0] if parts else ""
    if first and "=" not in first:
        identifier = first
        parts = parts[1:]
    if not _server_trusted(identifier, trusted_authserv_ids):
        return AuthVerdict(False, "The authentication result is not from a trusted server.")
    from_domain = _domain(from_address)
    if not from_domain or from_domain.endswith(".invalid"):
        return AuthVerdict(False, "The sender address could not be read.")
    for part in parts:
        match = _RESULT.match(part)
        if match is None or match.group(2).lower() != "pass":
            continue
        method = match.group(1).lower()
        properties = {k.lower(): v for k, v in _PROPERTY.findall(match.group(3))}
        if method == "dkim" and aligned(properties.get("header.d", ""), from_domain):
            return AuthVerdict(True, "dkim passed for the sender's domain.")
        if method == "dmarc":
            claimed = properties.get("header.from")
            if claimed is None or claimed.lower().strip(".") == from_domain:
                return AuthVerdict(True, "dmarc passed for the sender's domain.")
    return AuthVerdict(False, "The sender's domain was not verified (no dkim or dmarc pass).")


# --- who may instruct --------------------------------------------------------------------


@dataclass(frozen=True)
class Instruction:
    """An email accepted as an instruction."""

    who: str
    """`owner` or `other`."""
    kinds: frozenset[str]
    """Intent kinds an `other` sender may authorise at agent-evidence level; empty if ask-only."""
    sender: str = ""

    def covers(self, kind: str) -> bool:
        return self.who == "owner" or kind in self.kinds


class Refusal(Exception):
    """An email that is not an instruction, with a safe reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def evaluate(
    senders: AuthorisedSenders,
    from_address: str,
    headers: tuple[str, ...],
    sent_at: datetime,
    now: datetime,
) -> Instruction:
    """Accept the email as an instruction or raise `Refusal`."""
    address = from_address.strip().lower()
    other = next((o for o in senders.others if o.address == address), None)
    if address not in senders.owner and other is None:
        raise Refusal("The sender is not on the authorised list.")
    verdict = check_authentication(headers, address, senders.trusted_authserv_ids)
    if not verdict.passed:
        raise Refusal(verdict.reason)
    if now - sent_at > INSTRUCTION_MAX_AGE:
        raise Refusal("The email is too old to count as a current instruction.")
    if address in senders.owner:
        return Instruction("owner", frozenset(), address)
    assert other is not None
    return Instruction("other", frozenset(k for k in other.scope if k != ASK_ONLY), address)


# --- how an accepted instruction is recorded on evidence ---------------------------------


def instruction_ref(instruction: Instruction, message: str) -> str:
    """The `ref` of the evidence row: `instruction:<owner|other>:<kinds,>:<msg handle>`."""
    return f"{REF_PREFIX}:{instruction.who}:{','.join(sorted(instruction.kinds))}:{message}"


def parse_instruction_ref(ref: str | None) -> Instruction | None:
    """The instruction a stored `ref` records, or None for any other evidence."""
    if ref is None or not ref.startswith(REF_PREFIX + ":"):
        return None
    parts = ref.split(":", 3)
    if len(parts) != 4 or parts[1] not in ("owner", "other"):
        return None
    return Instruction(parts[1], frozenset(k for k in parts[2].split(",") if k))
