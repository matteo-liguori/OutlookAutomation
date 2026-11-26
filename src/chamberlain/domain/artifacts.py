"""Artifact kinds, states and owner kinds.

Contract: docs/contracts/06-memory.md (Artifacts and owners, M-4).
"""

from enum import StrEnum

from chamberlain.domain.handles import HandleKind

REPORT_OUTLINE_FILENAME = "report-outline.txt"
"""The stored item the harness keeps under a running pass: what the agent writes in its report.
It is not a report; every reader of a run's reports leaves it out (docs/contracts/06-memory.md)."""


class ArtifactKind(StrEnum):
    MAIL_BODY = "mail_body"
    ATTACHMENT = "attachment"
    DOWNLOAD = "download"
    IMPORT = "import"
    TEXT = "text"


class OwnerKind(StrEnum):
    """Who may hold an artifact (M-4). A card holds only the context carried over from the old
    harness (M-35); the agent cannot name a card as an owner."""

    ACTION = "action"
    OPERATION = "operation"
    GRANT = "grant"
    SESSION = "session"
    RUN = "run"
    CARD = "card"


OWNER_HANDLE: dict[OwnerKind, HandleKind] = {
    OwnerKind.ACTION: HandleKind.ACTION,
    OwnerKind.OPERATION: HandleKind.OPERATION,
    OwnerKind.GRANT: HandleKind.GRANT,
    OwnerKind.SESSION: HandleKind.SESSION,
    OwnerKind.RUN: HandleKind.RUN,
    OwnerKind.CARD: HandleKind.CARD,
}
"""Handle kind of each owner kind. A run is owned by the runs track (`runs` table)."""


def owner_kind_of(handle: str) -> OwnerKind | None:
    """The artifact owner kind a handle names, or None if it cannot own artifacts."""
    prefix = handle.partition("_")[0]
    for kind, known in OWNER_HANDLE.items():
        if known.value == prefix:
            return kind
    return None
