"""Grant kinds, states and the standing scope.

Contract: docs/contracts/05-grants.md (Kinds, Lifecycle, G-1, G-12).
"""

from enum import StrEnum
from typing import Any, Self, cast

from pydantic import Field, model_validator

from chamberlain.domain.intents import IntentKind, OutsideCategory
from chamberlain.schema.base import (
    OUTSIDE_SOURCE,
    CardHandle,
    EmailAddress,
    Model,
    SourceId,
    WriteSourceId,
)


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

    source: WriteSourceId
    sources: frozenset[SourceId] | None = None
    """Every source the grant covers, when it covers several of one role (G-26). Left out for one
    source, so a single-source scope reads and hashes as it always did. `source` is the first
    member in sorted order."""
    intent_kinds: frozenset[IntentKind] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=300)
    recipients: frozenset[EmailAddress] = frozenset()
    recipient_domains: frozenset[str] = frozenset()
    folders: frozenset[str] = frozenset()
    calendar: SourceId | None = None
    allow_mark_read: bool = False
    allow_repeat: bool = False
    allow_duplicate: bool = False
    """Let a send through the duplicate scan of `prepare` (board R3)."""
    card: CardHandle | None = None
    max_per_day: int | None = Field(default=None, ge=1, le=500)
    where: str | None = Field(default=None, min_length=1, max_length=80)
    """An outside action's site, portal or app (G-25)."""
    categories: frozenset[OutsideCategory] | None = Field(default=None, min_length=1)
    """The kinds of outside action covered (G-25)."""

    @model_validator(mode="before")
    @classmethod
    def _one_form_of_sources(cls, data: object) -> object:
        """The same set of sources always gives the same scope: `source` is its first member."""
        if not isinstance(data, dict):
            return data
        given = cast(dict[str, Any], data)
        listed = given.get("sources")
        if not listed:
            return given
        members: set[Any] = {*listed, *([given["source"]] if given.get("source") else [])}
        if not all(isinstance(item, str) for item in members):
            return given
        return {
            **given,
            "source": min(members),
            "sources": frozenset(members) if len(members) > 1 else None,
        }

    def all_sources(self) -> frozenset[str]:
        return self.sources if self.sources else frozenset({self.source})

    def covers(self, source: str) -> bool:
        """True when this scope is for `source` (matched per intent by the intent's own source)."""
        return source in self.all_sources()

    @model_validator(mode="after")
    def _outside_or_not(self) -> Self:
        outside = IntentKind.OUTSIDE_ACTION in self.intent_kinds
        if self.sources is not None:
            if len({s.partition(":")[0] for s in self.sources}) != 1:
                raise ValueError("one grant covers sources of one kind only (G-26)")
            if self.calendar is not None:
                raise ValueError("a grant for several sources cannot also name one calendar")
        if not outside:
            if self.source == OUTSIDE_SOURCE or self.where or self.categories:
                raise ValueError("only an outside-action scope names a place and categories")
            return self
        if (
            self.intent_kinds != frozenset({IntentKind.OUTSIDE_ACTION})
            or self.source != OUTSIDE_SOURCE
        ):
            raise ValueError("an outside-action scope holds nothing else (G-25)")
        if self.card is None or self.where is None or self.categories is None:
            raise ValueError(
                "a standing outside action names its card, place and categories (G-25)"
            )
        if not self.summary.strip():
            raise ValueError("a standing outside action says in plain words what it allows (G-25)")
        if (
            self.sources is not None
            or self.recipients
            or self.recipient_domains
            or self.folders
            or self.calendar
            or self.allow_mark_read
        ):
            raise ValueError("an outside-action scope has no sources, recipients or folders")
        return self

    @model_validator(mode="after")
    def _no_wildcards(self) -> Self:
        if IntentKind.MAIL_SEND in self.intent_kinds:
            if not (self.recipients or self.recipient_domains):
                raise ValueError("a standing send scope names recipients or domains (G-1)")
            if self.max_per_day is None:
                raise ValueError("a standing send scope sets max_per_day (G-1)")
        return self
