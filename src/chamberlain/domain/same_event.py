"""Telling one real event held on two calendars from two different events.

Owner ruling 2026-11-25: the same event entered by two people (the owner and Imogen each put it
in), or the same flight landing on two calendars, is one event. The clash check and the viewer's
calendar treat such copies as one, without asking anyone. Two copies are the same event when
their times match (start within 10 minutes, end within 30) and their titles say the same thing
(the words of one are mostly in the other, or both name the same flight or booking code), or their
times match exactly and they name the same place.

Titles and places are remote text: these helpers compare them in memory and never return them.
"""

import re
from datetime import datetime, timedelta

START_SLACK = timedelta(minutes=10)
END_SLACK = timedelta(minutes=30)
_WORD = re.compile(r"[a-z0-9]+")
_CODE = re.compile(r"\b[a-z]{2}\s?\d{2,4}\b|\b[a-z0-9]{6}\b")
"""A flight number (BA 123, U2 8123) or a six-character booking reference."""
_STOP = frozenset(
    [
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with", "from",
        "by", "my", "our", "your", "me", "i", "we", "you", "is", "are", "be", "meeting",
        "event", "call", "appt", "appointment", "booking", "reservation",
    ]
)  # fmt: skip


def words(title: str) -> frozenset[str]:
    """The words of a title that carry meaning, for comparison only."""
    return frozenset(w for w in _WORD.findall(title.casefold()) if w not in _STOP and len(w) > 1)


def codes(title: str) -> frozenset[str]:
    return frozenset(
        c.replace(" ", "") for c in _CODE.findall(title.casefold()) if any(ch.isdigit() for ch in c)
    )


def same_place(a: str, b: str) -> bool:
    """Both name a place, and the words of one are all in the other."""
    left, right = words(a), words(b)
    return bool(left) and bool(right) and (left <= right or right <= left)


def titles_agree(a: str, b: str) -> bool:
    if codes(a) & codes(b):
        return True
    left, right = words(a), words(b)
    if not left or not right:
        return False
    shared = len(left & right)
    return shared / min(len(left), len(right)) >= 0.6 or shared / len(left | right) >= 0.5


def same_event(
    a_start: datetime,
    a_end: datetime,
    a_title: str,
    a_location: str,
    b_start: datetime,
    b_end: datetime,
    b_title: str,
    b_location: str,
) -> bool:
    """True when two calendar entries are one real event (see the module text)."""
    if abs(a_start - b_start) > START_SLACK or abs(a_end - b_end) > END_SLACK:
        return False
    if titles_agree(a_title, b_title):
        return True
    exact = a_start == b_start and a_end == b_end
    return exact and same_place(a_location, b_location)
