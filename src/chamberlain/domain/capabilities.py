"""Capability declarations and source health.

Contract: docs/contracts/08-ports.md (Capabilities, P-1, P-2, P-5, P-12).
"""

from enum import StrEnum

from pydantic import Field

from chamberlain.schema.base import Model, SourceId, Timestamp


class CapabilityName(StrEnum):
    MAIL_READ = "mail.read"
    MAIL_SEARCH = "mail.search"
    MAIL_ATTACHMENTS = "mail.attachments"
    MAIL_SEND = "mail.send"
    MAIL_DRAFT = "mail.draft"
    MAIL_MOVE = "mail.move"
    MAIL_TRASH = "mail.trash"
    MAIL_MARK_READ = "mail.mark_read"
    CALENDAR_READ = "calendar.read"
    CALENDAR_WRITE = "calendar.write"
    CONTACTS_SEARCH = "contacts.search"


class VerificationLevel(StrEnum):
    LIVE_VERIFIED = "live_verified"
    IMPLEMENTED = "implemented"
    KNOWN_BROKEN = "known_broken"


class Capability(Model):
    name: CapabilityName
    level: VerificationLevel
    limits: dict[str, int] = Field(default_factory=dict)
    absence_provable: frozenset[str] = frozenset()
    """Intent kinds for which `reconcile` may return a proven `absent` (P-12)."""


class CapabilitySet(Model):
    source: SourceId
    capabilities: tuple[Capability, ...]

    def get(self, name: CapabilityName) -> Capability | None:
        """The usable capability of that name, or None (P-1)."""
        for capability in self.capabilities:
            if capability.name is name and capability.level is not VerificationLevel.KNOWN_BROKEN:
                return capability
        return None

    def names(self) -> list[str]:
        return [c.name.value for c in self.capabilities if self.get(c.name) is not None]


class HealthState(StrEnum):
    OK = "ok"
    STALE = "stale"
    SIGN_IN_NEEDED = "sign_in_needed"
    UNAVAILABLE = "unavailable"


class SourceHealth(Model):
    source: SourceId
    state: HealthState
    checked_at: Timestamp
    fresh_as_of: Timestamp | None = None
    """Up to when the connector vouches for completeness; never a guess (P-5)."""
