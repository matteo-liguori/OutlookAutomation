"""A long mail search answered by walking date windows from newest to oldest.

Thunderbird scans at most `max_scan` messages per query and returns an arbitrary subset of a
capped scan, so a search over years is cut into windows that each scan completely."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from chamberlain.connectors.thunderbird.protocol import Header
from chamberlain.domain.mail import bare

WALK_BUDGET_SECONDS: Final = 90.0
"""How long one search call may walk before it hands back a cursor to continue."""
FLOOR: Final = datetime(1990, 1, 1, tzinfo=UTC)
DAY_MS: Final = 86_400_000
FIRST_SPAN_MS: Final = 30 * DAY_MS
MAX_SPAN_MS: Final = 2 * 365 * DAY_MS
QUIET_SCAN: Final = 500
"""A complete window that scanned fewer messages than this makes the next window twice as wide."""

Fetch = Callable[[int, int], tuple[list[Header], bool, int]]
"""`(since_ms, until_ms)` to every matching header of that window, whether the scan was
complete, and how many messages it scanned."""


def sort_key(header: Header) -> tuple[int, str]:
    """The add-on's order: date, then folder and Message-ID."""
    return header.date_ms, f"{header.folder_id}\u0000{bare(header.internet_message_id)}"


@dataclass
class Walk:
    headers: list[Header] = field(default_factory=lambda: [])
    """Newest first, each message once."""
    reached_ms: int = 0
    """The walk covered everything from here up; below it is unsearched."""
    out_of_time: bool = False
    scan_limited: bool = False
    """A window under a day still stopped at the scan limit."""


def walk(
    fetch: Fetch,
    *,
    upper_ms: int,
    lower_ms: int,
    want: int,
    after: tuple[int, str] | None,
    budget: float = WALK_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    started: float | None = None,
) -> Walk:
    """Collect matches in [lower, upper) until more than `want` are found, the floor is reached
    or the time is spent. At least one window is always searched, so every call progresses."""
    begin = monotonic() if started is None else started
    if after is not None:
        upper_ms = min(upper_ms, after[0] + 1)
    found: dict[tuple[str, str], Header] = {}
    result = Walk(reached_ms=upper_ms)
    span = FIRST_SPAN_MS
    hi = upper_ms
    searched = False
    while hi > lower_ms and len(found) <= want:
        if searched and monotonic() - begin >= budget:
            result.out_of_time = True
            break
        lo = max(lower_ms, hi - span)
        headers, complete, scanned = fetch(lo, hi)
        searched = True
        if not complete:
            if span > DAY_MS:
                span = max(span // 2, DAY_MS)
                continue
            result.scan_limited = True
        for header in headers:
            if after is not None and sort_key(header) >= after:
                continue
            found.setdefault((header.folder_id, bare(header.internet_message_id)), header)
        hi = lo
        result.reached_ms = hi
        if complete and scanned < QUIET_SCAN:
            span = min(span * 2, MAX_SPAN_MS)
    result.headers = sorted(found.values(), key=sort_key, reverse=True)
    return result
