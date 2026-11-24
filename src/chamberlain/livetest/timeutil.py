"""One timestamp format for reports."""

from datetime import UTC, datetime


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
