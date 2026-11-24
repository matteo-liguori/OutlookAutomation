"""What an import did and what it left for the owner. Deterministic: no timestamps."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImportReport:
    counts: dict[str, int] = field(default_factory=dict[str, int])
    """Rows written this run, by kind (a second run writes none)."""
    already_there: dict[str, int] = field(default_factory=dict[str, int])
    sources: dict[str, str] = field(default_factory=dict[str, str])
    """Imported source id to a one-line description."""
    skipped_sources: list[str] = field(default_factory=list[str])
    secrets: dict[str, str] = field(default_factory=dict[str, str])
    """Secret reference to `copied`, `already present` or `differs; kept the existing one`."""
    uncertain_operations: list[str] = field(default_factory=list[str])
    standing_grants: list[str] = field(default_factory=list[str])
    over_cap_cards: list[str] = field(default_factory=list[str])
    private_guidance: dict[str, str] = field(default_factory=dict[str, str])
    """Old private instruction topics, with their text, for the owner to turn into rule patches."""
    unmapped_settings: dict[str, Any] = field(default_factory=dict[str, Any])
    left_behind: dict[str, int] = field(default_factory=dict[str, int])
    warnings: list[str] = field(default_factory=list[str])
    next_steps: list[str] = field(default_factory=list[str])
    config_written: str = ""
    """`created`, `already identical` or `kept existing (differs)`."""
    where: str = ""
    """The old record being read now, so a failure can name it. Not part of the document."""

    def count(self, kind: str, *, new: bool) -> None:
        target = self.counts if new else self.already_there
        target[kind] = target.get(kind, 0) + 1

    @property
    def changed(self) -> bool:
        return bool(self.counts)

    def document(self) -> dict[str, Any]:
        """The full private report, written to `<data root>/import/report.json`."""
        return {
            "counts": dict(sorted(self.counts.items())),
            "already_there": dict(sorted(self.already_there.items())),
            "sources": dict(sorted(self.sources.items())),
            "skipped_sources": sorted(self.skipped_sources),
            "secrets": dict(sorted(self.secrets.items())),
            "uncertain_operations": sorted(self.uncertain_operations),
            "standing_grants_need_reconfirmation": sorted(self.standing_grants),
            "over_cap_cards": sorted(self.over_cap_cards),
            "private_guidance_to_convert": dict(sorted(self.private_guidance.items())),
            "unmapped_settings": self.unmapped_settings,
            "left_behind": dict(sorted(self.left_behind.items())),
            "warnings": sorted(self.warnings),
            "next_steps": self.next_steps,
            "config": self.config_written,
        }

    def summary(self) -> dict[str, Any]:
        """What the command prints: counts and names, no private text."""
        document = self.document()
        document["private_guidance_to_convert"] = sorted(self.private_guidance)
        return document
