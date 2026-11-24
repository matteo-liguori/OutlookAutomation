"""Artifact kinds, states and owner kinds.

Contract: docs/contracts/06-memory.md (Artifacts and owners, M-4).
"""

from enum import StrEnum


class ArtifactKind(StrEnum):
    MAIL_BODY = "mail_body"
    ATTACHMENT = "attachment"
    DOWNLOAD = "download"
    IMPORT = "import"
    TEXT = "text"


class ArtifactState(StrEnum):
    LIVE = "live"
    RELEASED = "released"
    DELETED = "deleted"


class OwnerKind(StrEnum):
    """Who may hold an artifact (M-4). A card holds evidence only."""

    ACTION = "action"
    OPERATION = "operation"
    GRANT = "grant"
    SESSION = "session"
    RUN = "run"


OWNER_PREFIX: dict[OwnerKind, str] = {
    OwnerKind.ACTION: "act",
    OwnerKind.OPERATION: "op",
    OwnerKind.GRANT: "grant",
    OwnerKind.SESSION: "ses",
    OwnerKind.RUN: "run",
}
"""Handle prefix of each owner kind. A run is owned by the runs track (`runs` table)."""


def owner_kind_of(handle: str) -> OwnerKind | None:
    """The artifact owner kind a handle names, or None if it cannot own artifacts."""
    prefix = handle.partition("_")[0]
    for kind, known in OWNER_PREFIX.items():
        if known == prefix:
            return kind
    return None
