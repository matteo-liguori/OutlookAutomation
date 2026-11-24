"""Card and note rules that need no storage: sizes and the owner kinds of evidence.

Contract: docs/contracts/06-memory.md (Bounded records, M-12 to M-16).
"""

from enum import StrEnum

from chamberlain.limits import CARD_MAX_CHARS
from chamberlain.schema.tools import CardView


class EvidenceOwnerKind(StrEnum):
    CARD = "card"
    ACTION = "action"
    GRANT = "grant"
    OPERATION = "operation"


EVIDENCE_OWNER_PREFIX: dict[EvidenceOwnerKind, str] = {
    EvidenceOwnerKind.CARD: "card",
    EvidenceOwnerKind.ACTION: "act",
    EvidenceOwnerKind.GRANT: "grant",
    EvidenceOwnerKind.OPERATION: "op",
}


def evidence_owner_kind(handle: str) -> EvidenceOwnerKind | None:
    """The evidence owner kind a handle names, or None (T-10)."""
    prefix = handle.partition("_")[0]
    for kind, known in EVIDENCE_OWNER_PREFIX.items():
        if known == prefix:
            return kind
    return None


def card_size(card: CardView) -> int:
    """Characters of the serialised card with its open actions (M-12)."""
    return len(card.model_dump_json(exclude_none=True, exclude=_NOT_COUNTED))


_NOT_COUNTED = {"context", "complex"}
"""Fields that describe the card's surroundings; they never count against its size."""


def card_over_cap(card: CardView, limit: int = CARD_MAX_CHARS) -> bool:
    return card_size(card) > limit
