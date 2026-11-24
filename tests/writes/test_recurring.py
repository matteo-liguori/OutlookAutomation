"""Repeating events on the fake world: read each date, write a series, one date or the whole
series (08-ports P-23 to P-27, 04-writes W-42)."""

from datetime import UTC, datetime, timedelta

import pytest

from chamberlain.domain.calendar import (
    CalendarEvent,
    CalendarQuery,
    EventFields,
    EventStatus,
    Recurrence,
)
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import CalendarCreate, CalendarDelete, CalendarUpdate, WriteIntent
from chamberlain.domain.operations import OperationState
from chamberlain.domain.recurrence import describe, expand
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.testing.write_rig import CALENDAR, CALENDAR_PLAIN, OWNER_B, WriteRig, make_rig
from chamberlain.writes.models import WritePolicy
from chamberlain.writes.render import describe_intent

FIRST = datetime(2026, 3, 2, 9, tzinfo=UTC)  # a Monday
WINDOW = CalendarQuery(
    start=datetime(2026, 3, 1, tzinfo=UTC), end=datetime(2026, 4, 30, tzinfo=UTC), limit=250
)


def standup(*, title: str = "Standup", count: int = 4, **more: object) -> EventFields:
    rule = {"frequency": "weekly", "weekdays": ["mon"], "count": count}
    return EventFields.model_validate(
        {
            "title": title,
            "start": FIRST,
            "end": FIRST + timedelta(minutes=30),
            "recurrence": rule,
            **more,
        }
    )


def run(rig: WriteRig, intent: WriteIntent) -> tuple[str, str]:
    """Propose, approve with the owner's word, do. Returns the preview text and the state."""
    engine = rig.engine()
    proposal = engine.propose_intent(intent)
    assert proposal.requirement.value == "agent_evidence"
    approved = rig.agent_approves(proposal, "Yes, do that.")
    assert approved.operation is not None
    done = engine.do(approved.operation.handle)
    return proposal.preview, done.state.value


def dates(rig: WriteRig, *, include_cancelled: bool = False) -> list[CalendarEvent]:
    query = WINDOW.model_copy(update={"include_cancelled": include_cancelled})
    return list(rig.fake.calendar.events(CALENDAR, query).items)


def handle(rig: WriteRig, locator: str) -> str:
    return rig.refs.remember(HandleKind.CALENDAR_EVENT, CALENDAR, locator)


def test_a_series_is_read_as_dates_and_written_one_date_or_all(rig: WriteRig) -> None:
    # Create: the harness fills in the clock the times repeat on.
    preview, state = run(rig, CalendarCreate(source=CALENDAR, event=standup()))
    assert state == OperationState.CONFIRMED.value
    assert "Repeats: every week on Mon, 4 times" in preview
    series = dates(rig)[0].series_locator
    assert series is not None
    master = rig.fake.calendar.read(CALENDAR, series)
    assert master.is_series
    assert master.fields.recurrence is not None
    assert master.fields.recurrence.zone == "UTC"

    # Reading: each date is its own event, marked as part of the series, one etag for all.
    seen = dates(rig)
    assert [e.fields.start.day for e in seen] == [2, 9, 16, 23]
    assert all(e.is_occurrence and e.series_locator == series for e in seen)
    assert len({e.etag for e in seen}) == 1

    # One date changed: the others stay.
    second, third = seen[1], seen[2]
    preview, state = run(
        rig,
        CalendarUpdate(
            source=CALENDAR,
            event_ref=handle(rig, second.locator),
            expected_etag=second.etag,
            scope="occurrence",
            event=second.fields.model_copy(update={"title": "Standup (moved room)"}),
        ),
    )
    assert state == OperationState.CONFIRMED.value
    assert "this date only" in preview.lower()
    assert [e.fields.title for e in dates(rig)] == [
        "Standup",
        "Standup (moved room)",
        "Standup",
        "Standup",
    ]

    # One date cancelled: it reads cancelled, and the window no longer lists it.
    third = rig.fake.calendar.read(CALENDAR, third.locator)
    preview, state = run(
        rig,
        CalendarDelete(
            source=CALENDAR,
            event_ref=handle(rig, third.locator),
            expected_etag=third.etag,
            scope="occurrence",
        ),
    )
    assert state == OperationState.CONFIRMED.value
    assert "2026-03-16" in preview
    assert len(dates(rig)) == 3
    assert rig.fake.calendar.read(CALENDAR, third.locator).status is EventStatus.CANCELLED
    assert len(dates(rig, include_cancelled=True)) == 4

    # The whole series: no rule given keeps the rule; the other dates keep their own changes.
    master = rig.fake.calendar.read(CALENDAR, series)
    preview, state = run(
        rig,
        CalendarUpdate(
            source=CALENDAR,
            event_ref=handle(rig, series),
            expected_etag=master.etag,
            scope="series",
            event=master.fields.model_copy(update={"title": "Team standup", "recurrence": None}),
        ),
    )
    assert state == OperationState.CONFIRMED.value
    assert "every date" in preview.lower()
    titles = [e.fields.title for e in dates(rig)]
    assert titles == ["Team standup", "Standup (moved room)", "Team standup"]
    assert rig.fake.calendar.read(CALENDAR, series).fields.recurrence == master.fields.recurrence

    # The whole series deleted.
    master = rig.fake.calendar.read(CALENDAR, series)
    _, state = run(
        rig,
        CalendarDelete(
            source=CALENDAR,
            event_ref=handle(rig, series),
            expected_etag=master.etag,
            scope="series",
        ),
    )
    assert state == OperationState.CONFIRMED.value
    assert dates(rig) == []
    assert rig.fake.calendar.read(CALENDAR, series).status is EventStatus.CANCELLED


def test_the_scope_must_fit_the_handle_and_a_source_that_cannot_says_so(rig: WriteRig) -> None:
    run(rig, CalendarCreate(source=CALENDAR, event=standup()))
    occurrence = dates(rig)[0]
    series = occurrence.series_locator
    assert series is not None
    master = rig.fake.calendar.read(CALENDAR, series)
    engine = rig.engine()

    def refused(ref: str, etag: str, scope: str | None, **more: object) -> ChamberlainError:
        intent = CalendarDelete.model_validate(
            {
                "source": CALENDAR,
                "event_ref": ref,
                "expected_etag": etag,
                "scope": scope,
                **more,
            }
        )
        with pytest.raises(ChamberlainError) as caught:
            engine.propose_intent(intent)
        return caught.value

    # A repeating event needs the scope, and the fix names the handle to use.
    error = refused(handle(rig, occurrence.locator), occurrence.etag, None)
    assert error.code is ErrorCode.REQUEST_INVALID
    assert error.fix is not None
    assert "view.series" in error.fix
    assert refused(handle(rig, series), master.etag, "occurrence").code is ErrorCode.REQUEST_INVALID
    assert refused(handle(rig, series), master.etag, None).code is ErrorCode.REQUEST_INVALID

    # A single event takes none.
    single = rig.fake.calendar.add_event(
        CALENDAR, EventFields(title="Once", start=FIRST, end=FIRST + timedelta(hours=1))
    )
    one = rig.fake.calendar.read(CALENDAR, single)
    assert refused(handle(rig, single), one.etag, "series").code is ErrorCode.REQUEST_INVALID

    # No guests on a series, and a source that cannot write a series says so at propose.
    with pytest.raises(ChamberlainError) as guests:
        engine.propose_intent(CalendarCreate(source=CALENDAR, event=standup(attendees=[OWNER_B])))
    assert guests.value.detail == "recurring-invitations-not-supported"
    with pytest.raises(ChamberlainError) as plain:
        engine.propose_intent(CalendarCreate(source=CALENDAR_PLAIN, event=standup()))
    assert plain.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert plain.value.detail == "recurrence-not-supported"


def test_a_rule_repeats_on_its_own_clock_and_reads_in_plain_words() -> None:
    from zoneinfo import ZoneInfo

    london = Recurrence.model_validate(
        {"frequency": "weekly", "weekdays": ["wed", "mon"], "zone": "Europe/London", "count": 6}
    )
    first = datetime(2027, 3, 22, 9, tzinfo=ZoneInfo("Europe/London"))
    window = (datetime(2027, 3, 1, tzinfo=UTC), datetime(2027, 6, 1, tzinfo=UTC))
    found = expand(
        london,
        first,
        first + timedelta(hours=1),
        all_day=False,
        window_start=window[0],
        window_end=window[1],
    )
    local = [begin.astimezone(ZoneInfo("Europe/London")).hour for begin, _ in found]
    assert local == [9] * 6  # nine o'clock stays nine o'clock across the clock change
    assert found[-1][0].hour == 8  # which is eight UTC in summer
    assert describe(london).startswith("every week on Mon and Wed, 6 times")

    monthly = Recurrence.model_validate(
        {"frequency": "monthly", "until": datetime(2027, 12, 31, 23, tzinfo=UTC)}
    )
    long_months = expand(
        monthly,
        datetime(2027, 1, 31, 9, tzinfo=UTC),
        datetime(2027, 1, 31, 10, tzinfo=UTC),
        all_day=False,
        window_start=window[0] - timedelta(days=60),
        window_end=datetime(2028, 1, 1, tzinfo=UTC),
    )
    assert [b.month for b, _ in long_months] == [
        1,
        3,
        5,
        7,
        8,
        10,
        12,
    ]  # months without a 31st skip

    with pytest.raises(ValueError, match="until or count"):
        Recurrence.model_validate(
            {"frequency": "daily", "count": 2, "until": datetime(2027, 1, 1, tzinfo=UTC)}
        )
    shown = describe_intent(CalendarCreate(source=CALENDAR, event=standup()))[0]
    assert "Create a repeating calendar event" in shown


def test_the_series_clock_is_the_owners_configured_zone() -> None:
    rig = make_rig(policy=WritePolicy(zone="Europe/London"))
    try:
        run(rig, CalendarCreate(source=CALENDAR, event=standup()))
        series = dates(rig)[0].series_locator
        assert series is not None
        rule = rig.fake.calendar.read(CALENDAR, series).fields.recurrence
        assert rule is not None
        assert rule.zone == "Europe/London"
    finally:
        rig.close()
