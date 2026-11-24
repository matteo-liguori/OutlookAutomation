"""Which size limits apply to a card: the normal ones, or the larger ones the owner approved.

Contract: docs/contracts/06-memory.md (M-12, M-36).
"""

import sqlite3
from dataclasses import dataclass

from chamberlain.limits import (
    CARD_MAX_CHARS,
    CARD_POSITION_MAX,
    COMPLEX_CARD_MAX_CHARS,
    COMPLEX_POSITION_MAX,
)
from chamberlain.memory import repo


@dataclass(frozen=True)
class Caps:
    position: int
    card: int


NORMAL = Caps(CARD_POSITION_MAX, CARD_MAX_CHARS)
COMPLEX = Caps(COMPLEX_POSITION_MAX, COMPLEX_CARD_MAX_CHARS)


def status_of(db: sqlite3.Connection, card: str) -> str | None:
    """`pending`, `approved` or `declined`, or None when the card never asked."""
    row = repo.get_complex(db, card)
    return None if row is None else str(row["state"])


def caps_for(db: sqlite3.Connection, card: str) -> Caps:
    """The limits that apply to this card now."""
    return COMPLEX if status_of(db, card) == "approved" else NORMAL
