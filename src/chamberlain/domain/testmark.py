"""The marker every live-test item carries, and how a title is recognised as marked.

Contract: docs/live-test.md (What it touches). Daily operation uses it to keep live-test
calendar events out of its views; the live test's sweep uses it to remove only its own items.
"""

import re

MARKER = "CHAMBERLAIN LIVE TEST"
_REPLY_PREFIX = re.compile(r"^\s*((re|fwd?|aw|sv)\s*:\s*)+", re.IGNORECASE)
_NOTICE_PREFIX = re.compile(
    r"^\s*(?:(?:updated\s+)?invitation(?:\s+from\s+[^:]{1,80})?\s*:"
    r"|(?:tentatively\s+)?accepted\s*:|declined\s*:|cancell?ed\s+(?:event|invitation)\s*:"
    r"|[^:]{1,80}?\binvited you to)\s*[\"'“‘]?",  # noqa: RUF001 - curly quotes are what iCloud sends
    re.IGNORECASE,
)
"""What a calendar puts before an invitation's title in its email (Google: "Invitation:",
"Accepted:"; iCloud: "<name> invited you to “...”", found live 2026-11-25)."""
_OPEN_QUOTE = re.compile(r"^\s*[\"'“‘]")  # noqa: RUF001
_MARKED = re.compile(rf"{re.escape(MARKER)} (\d{{8}}T\d{{6}}-[0-9a-f]{{4}})\b")


def run_of(text: str) -> str | None:
    """The run id inside a marked subject, or None when the text carries no marker."""
    found = _MARKED.search(_REPLY_PREFIX.sub("", text, count=1))
    return found.group(1) if found else None


def carries_marker(subject: str) -> bool:
    """True for `CHAMBERLAIN LIVE TEST <run id> ...` after any `Re:` or `Fwd:` prefixes.

    The marker must open the subject (after reply prefixes, or a calendar's notice words such
    as "Accepted:"): a message that merely mentions the words is not ours.
    """
    stripped = _REPLY_PREFIX.sub("", subject, count=1).lstrip()
    # A calendar's own email about a marked event: its notice words, then the marked title.
    stripped = _OPEN_QUOTE.sub("", _NOTICE_PREFIX.sub("", stripped, count=1), count=1).lstrip()
    return stripped.startswith(MARKER + " ") and run_of(stripped) is not None


DELIVERY_MARK = "CHAMBERLAIN DELIVERY CHECK"
"""Opens the subject of the message the fallback delivery check sends from a mailbox to itself
(docs/configuration.md, `mail_sync`). The daily mail scan leaves such messages out."""


def carries_delivery_mark(subject: str) -> bool:
    """True for `CHAMBERLAIN DELIVERY CHECK <source> <time>` after any `Re:` or `Fwd:` prefixes."""
    return _REPLY_PREFIX.sub("", subject, count=1).lstrip().startswith(DELIVERY_MARK + " ")
