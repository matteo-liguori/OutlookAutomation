"""CalDAV connector against a real Radicale server (CalendarPortContract)."""

import base64

import pytest
from cal_radicale import PASSWORD, USER, RadicaleServer
from cal_rigs import CALDAV_ADDRESS, CalDavRig, FaultyTransport

from chamberlain.connectors.caldav.calendar import CalDavCalendar
from chamberlain.connectors.caldav.config import CalDavBinding, CalDavConfig
from chamberlain.connectors.calendar_support.http import UrllibTransport
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import ManualClock
from chamberlain.testing.calendar_contract import CalendarPortContract
from chamberlain.testing.rigs import CalendarRig


def build(
    server: RadicaleServer, clock: ManualClock
) -> tuple[CalDavCalendar, FaultyTransport, str]:
    collection = server.new_calendar()
    transport = FaultyTransport(UrllibTransport())
    config = CalDavConfig(
        bindings={
            "calendar:test": CalDavBinding(
                url=collection, username=USER, credential="test", address=CALDAV_ADDRESS
            )
        }
    )
    port = CalDavCalendar(config, lambda binding: PASSWORD, clock, transport)
    return port, transport, collection


def basic() -> str:
    return "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


class TestCalDavContract(CalendarPortContract):
    @pytest.fixture
    def rig(self, radicale: RadicaleServer) -> CalendarRig:
        clock = ManualClock()
        port, transport, collection = build(radicale, clock)
        assert isinstance(port, CalendarPort)
        return CalDavRig(port, transport, clock, collection, basic())
