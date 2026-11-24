"""Calendar types exchanged across CalendarPort.

Contract: docs/contracts/08-ports.md (P-16).
"""

from enum import StrEnum
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from chamberlain.limits import RECURRENCE_COUNT_MAX, RECURRENCE_INTERVAL_MAX, RECURRENCE_ZONE_MAX
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


class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


WEEKDAY_ORDER = tuple(Weekday)


class Recurrence(Model):
    """A plain repeat rule (P-23): every `interval` days, weeks, months or years.

    Weekly rules may name the weekdays; monthly and yearly rules repeat on the first
    occurrence's day of the month (and month). With neither `until` nor `count` the series
    never ends. `zone` is the clock the times repeat on: nine o'clock stays nine o'clock
    across a clock change. The harness fills it in when the agent leaves it out.
    """

    frequency: Frequency
    interval: int = Field(default=1, ge=1, le=RECURRENCE_INTERVAL_MAX)
    weekdays: tuple[Weekday, ...] = ()
    until: Timestamp | None = None
    count: int | None = Field(default=None, ge=1, le=RECURRENCE_COUNT_MAX)
    zone: str | None = Field(default=None, max_length=RECURRENCE_ZONE_MAX)

    @field_validator("weekdays")
    @classmethod
    def _sorted_weekdays(cls, value: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
        return tuple(sorted(set(value), key=WEEKDAY_ORDER.index))

    @field_validator("zone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("unknown time zone") from None
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.until is not None and self.count is not None:
            raise ValueError("give until or count, not both")
        if self.weekdays and self.frequency is not Frequency.WEEKLY:
            raise ValueError("weekdays belong to a weekly rule")
        return self


class EventFields(Model):
    """What a calendar write sets. Part of the intent, so part of the hash."""

    title: str = Field(min_length=1, max_length=300)
    start: Timestamp
    end: Timestamp
    all_day: bool = False
    location: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=4000)
    attendees: tuple[EmailAddress, ...] = ()
    recurrence: Recurrence | None = None
    """Set on a series (P-23). On a read it is the series' rule when it is a plain one."""

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("end is before start")
        rule = self.recurrence
        if rule is not None and rule.until is not None and rule.until < self.start:
            raise ValueError("the series ends before it starts")
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
    series_locator: str | None = None
    """Part of a series (P-24): the locator of the series itself. Equal to `locator` on the
    series, different on one occurrence, None on a single event. Read only."""

    @property
    def is_series(self) -> bool:
        return self.series_locator is not None and self.series_locator == self.locator

    @property
    def is_occurrence(self) -> bool:
        return self.series_locator is not None and self.series_locator != self.locator


class CalendarQuery(Model):
    start: Timestamp
    end: Timestamp
    text: str | None = None
    include_cancelled: bool = False
    limit: int = Field(default=50, ge=1, le=250)
