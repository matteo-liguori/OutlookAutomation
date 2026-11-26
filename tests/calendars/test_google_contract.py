"""Google Calendar connector against an in-memory fake of the API (CalendarPortContract)."""

import pytest
from cal_fake_google import FakeGoogleApi
from cal_rigs import GoogleRig

from chamberlain.connectors.google_calendar.calendar import GoogleCalendar
from chamberlain.connectors.google_calendar.config import GoogleBinding, GoogleCalendarConfig
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import ManualClock
from support.calendar_contract import CalendarPortContract
from support.rigs import CalendarRig


def build(clock: ManualClock) -> tuple[GoogleCalendar, FakeGoogleApi]:
    api = FakeGoogleApi(clock)
    config = GoogleCalendarConfig(
        bindings={
            "calendar:test": GoogleBinding(account="me@example.test", calendar_id=api.calendar_id)
        }
    )
    return GoogleCalendar(config, lambda account: "token", clock, api), api


class TestGoogleCalendarContract(CalendarPortContract):
    @pytest.fixture
    def rig(self) -> CalendarRig:
        clock = ManualClock()
        port, api = build(clock)
        assert isinstance(port, CalendarPort)
        return GoogleRig(port, api, clock)
