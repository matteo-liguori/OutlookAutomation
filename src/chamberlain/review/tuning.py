"""Numbers that belong to the review track alone.

Contract: docs/contracts/06-memory.md (M-40). When a review opens is set by the owner (the
profile's `review` section, `rounds.RoundRule`); the rest are the track's own.
"""

from datetime import timedelta
from typing import Final

SCAN_EVERY: Final = timedelta(hours=24)
"""Outside a pass, the transcripts are read at most this often. After a pass ends they are read
once more at the next tick."""
LOOKBACK: Final = timedelta(days=30)
"""A transcript file not touched for this long is not read the first time it is seen."""
BYTES_PER_SCAN: Final = 64 * 1024 * 1024
"""One scan reads at most this much; the rest waits for the next one."""
FILES_PER_SCAN: Final = 2_000
HEAD_BYTES: Final = 64 * 1024
"""How much of the start of a file is looked at to find the folder the session ran in."""
MAX_LINE_BYTES: Final = 8 * 1024 * 1024
PATTERNS_MAX: Final = 500
PATTERN_KEEP: Final = timedelta(days=365)
FILE_KEEP: Final = timedelta(days=120)
ROUND_KEEP: Final = timedelta(days=365)
ROUND_LAPSE: Final = timedelta(days=14)
FINDING_MAX: Final = 400
FINDINGS_PER_ROUND: Final = 20
BRIEF_ROWS: Final = 60
SUMMARY_MAX: Final = 60
