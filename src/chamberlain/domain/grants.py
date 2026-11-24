"""Grant kinds, states and the standing scope.

Contract: docs/contracts/05-grants.md (Kinds, Lifecycle, G-1, G-12).
"""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from chamberlain.domain.intents import IntentKind
from chamberlain.schema.base import CardHandle, EmailAddress, Model, SourceId


class GrantKind(StrEnum):
    ONE_OFF = "one_off"
    STANDING = "standing"


class GrantState(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    SPENT = "spent"
    REVOKED = "revoked"
    EXPIRED = "expired"
    DECLINED = "declined"
    LAPSED = "lapsed"


_G = GrantState

GRANT_TRANSITIONS: dict[GrantState, frozenset[GrantState]] = {
    _G.PROPOSED: frozenset({_G.ACTIVE, _G.DECLINED, _G.LAPSED}),
    _G.ACTIVE: frozenset({_G.SPENT, _G.REVOKED, _G.EXPIRED, _G.SUSPENDED}),
    _G.SUSPENDED: frozenset({_G.ACTIVE, _G.REVOKED, _G.EXPIRED}),
    _G.SPENT: frozenset(),
    _G.REVOKED: frozenset(),
    _G.EXPIRED: frozenset(),
    _G.DECLINED: frozenset(),
    _G.LAPSED: frozenset(),
}

TERMINAL_GRANT_STATES = frozenset(s for s, targets in GRANT_TRANSITIONS.items() if not targets)
LIVE_GRANT_STATES = frozenset({_G.PROPOSED, _G.ACTIVE, _G.SUSPENDED})
"""A grant in one of these states is a live owner (M-4)."""


class Requirement(StrEnum):
    """What it takes to activate a grant (G-12 to G-14)."""

    NONE = "none"
    AGENT_EVIDENCE = "agent_evidence"
    HUMAN_CONFIRMATION = "human_confirmation"


class ConfirmedVia(StrEnum):
    AGENT_EVIDENCE = "agent_evidence"
    HUMAN_VIEWER = "human_viewer"
    IMPORT = "import"


class StandingScope(Model):
    """What a standing grant covers. Matching is exact-field, never fuzzy (W-22)."""

    source: SourceId
    intent_kinds: frozenset[IntentKind] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=300)
    recipients: frozenset[EmailAddress] = frozenset()
    recipient_domains: frozenset[str] = frozenset()
    folders: frozenset[str] = frozenset()
    calendar: SourceId | None = None
    allow_mark_read: bool = False
    allow_repeat: bool = False
    card: CardHandle | None = None
    max_per_day: int | None = Field(default=None, ge=1, le=500)

    @model_validator(mode="after")
    def _no_wildcards(self) -> Self:
        if IntentKind.MAIL_SEND in self.intent_kinds:
            if not (self.recipients or self.recipient_domains):
                raise ValueError("a standing send scope names recipients or domains (G-1)")
            if self.max_per_day is None:
                raise ValueError("a standing send scope sets max_per_day (G-1)")
        return self
