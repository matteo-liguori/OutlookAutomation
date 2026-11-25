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
    """Owner instruction topics added to `config/owner-instructions.yaml` this run (the text stays
    in that file; the report document lists names only)."""
    old_default_instructions: list[str] = field(default_factory=list[str])
    """Old instruction topics that were only the old harness's shipped text, so not carried."""
    read_state: dict[str, Any] = field(default_factory=dict[str, Any])
    """The old read-state settings, what they became, and the standing grants proposed."""
    appointments: list[str] = field(default_factory=list[str])
    """Important appointments imported or backfilled."""
    kept_as_changed: list[str] = field(default_factory=list[str])
    """Things a repeat import did not touch because the owner or agent changed them since."""
    unmapped_settings: dict[str, Any] = field(default_factory=dict[str, Any])
    left_behind: dict[str, int] = field(default_factory=dict[str, int])
    warnings: list[str] = field(default_factory=list[str])
    next_steps: list[str] = field(default_factory=list[str])
    recipients: dict[str, Any] = field(default_factory=dict[str, Any])
    """Per mail source: Sent-history seeding result (added, scanned, known, complete)."""
    config_written: str = ""
    """`created`, `already identical` or `kept existing (differs)`."""
    report_template: str = ""
    """The same for `config/report-template.yaml`; empty when the old install had no template."""
    where: str = ""
    """The old record being read now, so a failure can name it. Not part of the document."""

    def count(self, kind: str, *, new: bool) -> None:
        target = self.counts if new else self.already_there
        target[kind] = target.get(kind, 0) + 1

    @property
    def changed(self) -> bool:
        return (
            bool(self.counts)
            or self.report_template == "created"
            or self.config_written == "created"
        )

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
            "owner_instructions_added": sorted(self.private_guidance),
            "old_default_instructions_not_carried": sorted(self.old_default_instructions),
            "read_state": self.read_state,
            "important_appointments": sorted(self.appointments),
            "kept_because_changed_since": sorted(self.kept_as_changed),
            "unmapped_settings": self.unmapped_settings,
            "left_behind": dict(sorted(self.left_behind.items())),
            "warnings": sorted(self.warnings),
            "recipients_seeded": dict(sorted(self.recipients.items())),
            "next_steps": self.next_steps,
            "config": self.config_written,
            "report_template": self.report_template,
        }

    def summary(self) -> dict[str, Any]:
        """What the command prints: counts and names, no private text."""
        document = self.document()
        return document
