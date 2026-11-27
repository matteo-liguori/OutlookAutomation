"""The marker every live-test item carries, and how it is recognised.

Contract: docs/live-test.md (What it touches). The sweep removes only items whose subject
or title carries the marker; nothing else is ever matched.
"""

import re
from datetime import datetime

from chamberlain.domain.testmark import MARKER, carries_marker, run_of

__all__ = ["MARKER", "carries_marker", "run_of"]

RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{4}$")
CARD_TITLE = f"{MARKER} card"
GRANT_TITLE_PREFIX = f"{MARKER} grant"


def new_run_id(now: datetime, entropy: str) -> str:
    """`20261124T153000-ab12`: sortable, and unique enough for one owner."""
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{entropy[:4]}"


def run_marker(run_id: str) -> str:
    return f"{MARKER} {run_id}"


def grant_title(source: str) -> str:
    return f"{GRANT_TITLE_PREFIX} {source}"[:80]
