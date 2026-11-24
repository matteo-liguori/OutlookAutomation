"""Small pure helpers: deterministic handles, timestamps, text limits.

A new handle is derived from the old id, so a second import finds the same rows and skips
them: that is the whole idempotency mechanism, with no ledger table.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard
from zoneinfo import ZoneInfo

from chamberlain.memory.timeutil import ts

NAMESPACE = "chamberlain-import-v1"
ELLIPSIS = "…"


def derive(prefix: str, *parts: str) -> str:
    """`<prefix>_<16 hex>` from the old identity. Stable across runs and machines."""
    digest = hashlib.sha256("\n".join((NAMESPACE, prefix, *parts)).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:16]}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def norm_ts(value: Any, zone: ZoneInfo) -> str | None:
    """An old timestamp in the stored form, or None. A time with no offset is local to `zone`."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return ts(moment.astimezone(UTC))


def add_days(stamp: str, days: float) -> str:
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return ts(moment + timedelta(days=days))


def shorten(text: str, limit: int) -> str:
    """Cut to `limit` characters, marking the cut. Used only where a hard limit forces it."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + ELLIPSIS
