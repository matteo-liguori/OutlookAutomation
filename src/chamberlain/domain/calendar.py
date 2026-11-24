"""Calendar types exchanged across CalendarPort.

Contract: docs/contracts/08-ports.md (P-16).
"""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from chamberlain.schema.base import EmailAddress, Model, SourceId, Timestamp


class EventStatus(StrEnum):
    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    CANCELLED = "cancelled"


class EventFields(Model):
    """What a calendar write sets. Part of the intent, so part of the hash."""

    title: str = Field(min_length=1, max_length=300)
    start: Timestamp
    end: Timestamp
    all_day: bool = False
    location: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=4000)
    attendees: tuple[EmailAddress, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("end is before start")
        return self


class CalendarEvent(Model):
    source: SourceId
    locator: str
    uid: str
    etag: str
    status: EventStatus
    fields: EventFields
    updated: Timestamp


class CalendarQuery(Model):
    start: Timestamp
    end: Timestamp
    text: str | None = None
    include_cancelled: bool = False
    limit: int = Field(default=50, ge=1, le=250)
