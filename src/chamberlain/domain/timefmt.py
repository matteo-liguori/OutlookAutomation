"""Stored timestamps (D-7) and the fixed windows of the lifetime table."""

from datetime import UTC, datetime, timedelta


def ts(moment: datetime) -> str:
    """The stored form: UTC, second precision, suffix Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def plus(moment: datetime, *, days: float = 0, hours: float = 0, minutes: float = 0) -> str:
    return ts(moment + timedelta(days=days, hours=hours, minutes=minutes))


def minus(moment: datetime, *, days: float = 0, hours: float = 0, minutes: float = 0) -> str:
    return ts(moment - timedelta(days=days, hours=hours, minutes=minutes))
