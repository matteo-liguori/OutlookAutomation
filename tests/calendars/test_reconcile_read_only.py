"""Reconcile never reissues a write, for every calendar connector (ported calendar rows).

Old tests: `calendar_timeout_settles_only_from_exact_readback_without_reissuing`,
`calendar_reconcile_settles_a_late_write_without_reissuing_it` (docs/contracts/11).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from cal_bridge import FakeCalendarBridge
from cal_owl_rig import OwlRig
from cal_radicale import RadicaleServer
from cal_rigs import CalDavRig, GoogleRig
from test_caldav_contract import basic
from test_caldav_contract import build as build_caldav
from test_google_contract import build as build_google
from test_owl_calendar import build as build_owl

from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.intents import CalendarCreate
from chamberlain.domain.operations import DispatchOutcome, ReconcileStatus
from chamberlain.ports.clock import ManualClock
from support.rigs import CalendarRig

START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
Kit = tuple[CalendarRig, Callable[[], int]]


def google(radicale: RadicaleServer) -> Kit:
    clock = ManualClock()
    port, api = build_google(clock)
    rig = GoogleRig(port, api, clock)
    return rig, lambda: len([c for c in api.calls if c[0] != "GET"])


def caldav(radicale: RadicaleServer) -> Kit:
    clock = ManualClock()
    port, transport, collection = build_caldav(radicale, clock)
    rig = CalDavRig(port, transport, clock, collection, basic())
    return rig, lambda: len([s for s in transport.sent if s[0] in ("PUT", "DELETE", "POST")])


def owl(radicale: RadicaleServer) -> Kit:
    port, bridge, clock = build_owl()
    assert isinstance(bridge, FakeCalendarBridge)
    return OwlRig(port, bridge, clock), lambda: len([r for r in bridge.requests if r[1]])


@pytest.fixture(params=[google, caldav, owl], ids=["google", "caldav", "owl"])
def kit(request: pytest.FixtureRequest, radicale: RadicaleServer) -> Kit:
    return request.param(radicale)


def _event() -> EventFields:
    return EventFields(title="Once", start=START, end=START + timedelta(minutes=30))


def test_w10_a_lost_answer_is_settled_by_reading_back_without_a_second_write(kit: Kit) -> None:
    rig, mutations = kit
    uid = rig.new_uid()
    prepared = rig.port.prepare(CalendarCreate(source=rig.source, event=_event(), uid=uid), {})
    assert rig.inject(Fault.LOSE_RESPONSE_AFTER_APPLY)
    assert rig.port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    after_dispatch = mutations()
    assert after_dispatch == 1
    for _ in range(2):
        assert rig.port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT
    assert mutations() == after_dispatch
    assert rig.count(uid) == 1


def test_p11_a_late_write_is_found_by_reconcile_which_never_writes(kit: Kit) -> None:
    rig, mutations = kit
    uid = rig.new_uid()
    prepared = rig.port.prepare(CalendarCreate(source=rig.source, event=_event(), uid=uid), {})
    assert rig.inject(Fault.SLOW)
    assert rig.port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    before = mutations()
    rig.port.reconcile(prepared, "k")
    assert mutations() == before
    rig.advance(600)
    assert rig.port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT
    assert rig.count(uid) == 1
