"""Reconcile never reissues a write, for every calendar connector (ported calendar rows).

Old tests: `calendar_timeout_settles_only_from_exact_readback_without_reissuing`,
`calendar_reconcile_settles_a_late_write_without_reissuing_it` (docs/contracts/11).
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cal_bridge import FakeCalendarBridge
from cal_owl_rig import OwlRig
from cal_radicale import RadicaleServer
from cal_rigs import CalDavRig, GoogleRig
from test_caldav_contract import basic
from test_caldav_contract import build as build_caldav
from test_google_contract import build as build_google
from test_owl_calendar import build as build_owl

from chamberlain.connectors.calendar_support.port import payload
from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.intents import CalendarCreate
from chamberlain.domain.operations import DispatchOutcome, Prepared, ReconcileStatus
from chamberlain.ports.clock import ManualClock
from chamberlain.store.db import MEMORY, connect
from chamberlain.store.migrate import discover, migrate
from chamberlain.testing.rigs import CalendarRig

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


def test_migrated_old_create_reconciles_without_redispatch(
    kit: Kit, request: pytest.FixtureRequest
) -> None:
    rig, mutations = kit
    connector = {"google": "google_calendar", "caldav": "caldav", "owl": "thunderbird"}[
        request.node.callspec.id  # type: ignore[attr-defined]
    ]
    start = datetime(2027, 3, 1, 10, tzinfo=UTC)
    event = EventFields(
        title="Existing test appointment", start=start, end=start + timedelta(minutes=30)
    )
    uid = rig.new_uid()
    prepared = rig.port.prepare(CalendarCreate(source=rig.source, event=event, uid=uid), {})
    # Capture the equivalent old flat plan, before the single write happens.
    data: dict[str, Any] = dict(payload(prepared))
    data["fields"] = event.model_dump(mode="json")
    data.pop("kind", None)
    old = {
        "source": rig.source,
        "intent_kind": "calendar.create",
        "reconcile_token": uid,
        "data": data,
    }
    assert rig.port.dispatch(prepared, "one-write").outcome is DispatchOutcome.APPLIED
    before = mutations()

    db, clock = connect(MEMORY), ManualClock()
    try:
        migrate(db, clock, backup_dir=None, migrations=[m for m in discover() if m.version < 260])
        db.execute(
            "INSERT INTO sources (id,kind,connector,created_at,updated_at) "
            "VALUES (?, 'calendar', ?, 'now','now')",
            (rig.source, connector),
        )
        db.execute(
            "INSERT INTO operations (handle,idempotency_key,kind,source_id,state,intent_sha256,"
            "prepared_json,created_at,updated_at) "
            "VALUES ('op','key','calendar.create',?,'uncertain',?,?, 'now','now')",
            (rig.source, "0" * 64, json.dumps(old)),
        )
        migrate(db, clock, backup_dir=None)
        saved = db.execute("SELECT prepared_json FROM operations WHERE handle='op'").fetchone()[0]
        recovered = Prepared.model_validate_json(saved)
        assert rig.port.reconcile(recovered, "one-write").status is ReconcileStatus.PRESENT
        assert rig.port.reconcile(recovered, "one-write").status is ReconcileStatus.PRESENT
        assert mutations() == before
    finally:
        db.close()
