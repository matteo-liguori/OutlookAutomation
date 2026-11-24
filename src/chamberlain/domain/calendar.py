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


class Response(StrEnum):
    """An invitee's answer to an invitation."""

    NEEDS_ACTION = "needs_action"
    ACCEPTED = "accepted"
    TENTATIVE = "tentative"
    DECLINED = "declined"


class Participant(Model):
    """One invited person and the answer this calendar holds for them."""

    address: EmailAddress
    response: Response = Response.NEEDS_ACTION


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
    organiser: EmailAddress | None = None
    """Who sent the invitation, when the event has one. Read only; never part of a write."""
    my_response: Response | None = None
    """This calendar owner's answer on THIS copy, or None when they are not an invitee."""
    participants: tuple[Participant, ...] = ()
    """Each invitee's answer as this calendar knows it. On the organiser's copy this is how
    replies show. Read only; the attendee list a write sets is `fields.attendees`."""


class CalendarQuery(Model):
    start: Timestamp
    end: Timestamp
    text: str | None = None
    include_cancelled: bool = False
    limit: int = Field(default=50, ge=1, le=250)
