"""Shared calendar lifecycle: one mutation, conservative settlement and no replay."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from chamberlain.connectors.calendar_support.port import (
    CalendarPort,
    Current,
    Observation,
    WriteFailure,
    WriteReply,
    expected_etag,
    fields,
    locator,
    payload,
)
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.intents import CalendarCreate, CalendarDelete, CalendarUpdate
from chamberlain.domain.operations import (
    DispatchOutcome,
    ProviderReceipt,
    ReconcileStatus,
    Verification,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode

SOURCE = "calendar:test"
HANDLE = "cev_0123456789abcdef"
NOW = datetime(2027, 1, 1, tzinfo=UTC)
EVENT = EventFields(title="one", start=NOW, end=NOW + timedelta(hours=1))
RECEIPT = ProviderReceipt(locator="remote-id", verification=Verification(client_verified=True))


class Backend:
    def __init__(self) -> None:
        self.current = Current("remote-id", "v1", raw={"unmodeled": True})
        self.observation = Observation(True, matches=True, receipt=RECEIPT)
        self.writes = 0
        self.observes = 0
        self.preflight_error: Exception | None = None
        self.write_error: Exception | None = None
        self.reply = WriteReply(hint={"id": "actual"})
        self.seen_reply: WriteReply | None = None

    def resolve(self, source: str, locator: str) -> Current:
        assert source == SOURCE
        assert locator == "remote-id"
        return self.current

    def validate(self, intent: Any, current: Current | None) -> None:
        pass

    def encode(self, intent: Any, current: Current | None) -> dict[str, Any]:
        return {"provider_body": "encoded"}

    def preflight(self, prepared: Any) -> None:
        if self.preflight_error:
            raise self.preflight_error

    def write(self, prepared: Any) -> WriteReply:
        self.writes += 1
        if self.write_error:
            raise self.write_error
        return self.reply

    def observe(self, prepared: Any, reply: WriteReply | None) -> Observation:
        self.observes += 1
        self.seen_reply = reply
        return self.observation

    def capabilities(self, source: str) -> Any: ...
    def health(self, source: str) -> Any: ...
    def events(self, source: str, query: Any, cursor: str | None = None) -> Any: ...
    def read(self, source: str, locator: str) -> Any: ...


def make_create(port: CalendarPort):
    return port.prepare(CalendarCreate(source=SOURCE, event=EVENT, uid="uid"), {})


def make_update(port: CalendarPort):
    return port.prepare(
        CalendarUpdate(source=SOURCE, event_ref=HANDLE, expected_etag="v1", event=EVENT),
        {HANDLE: "remote-id"},
    )


def test_prepare_validates_and_records_the_common_facts() -> None:
    backend = Backend()
    port = CalendarPort(backend)
    prepared = make_update(port)
    assert payload(prepared) == {"provider_body": "encoded"}
    assert fields(prepared) == EVENT
    assert locator(prepared) == "remote-id"
    assert expected_etag(prepared) == "v1"
    assert make_create(port).reconcile_token == "uid"
    assert backend.writes == 0
    backend.current = Current("remote-id", "v2")
    with pytest.raises(ChamberlainError) as raised:
        make_update(port)
    assert raised.value.code is ErrorCode.REMOTE_CHANGED
    backend.current = Current("remote-id", "v1", part="series")
    with pytest.raises(ChamberlainError) as raised:
        make_update(port)
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_dispatch_only_applies_after_matching_readback_and_reconcile_never_writes() -> None:
    backend = Backend()
    port = CalendarPort(backend)
    prepared = make_create(port)
    assert port.dispatch(prepared, "a").outcome is DispatchOutcome.APPLIED
    assert backend.writes == 1
    assert backend.seen_reply == backend.reply
    assert port.reconcile(prepared, "a").status is ReconcileStatus.PRESENT
    assert backend.writes == 1
    assert backend.seen_reply is None
    backend.observation = Observation(True, matches=False, identity_suffices=True, receipt=RECEIPT)
    assert port.dispatch(prepared, "b").outcome is DispatchOutcome.UNKNOWN
    assert port.reconcile(prepared, "b").status is ReconcileStatus.PRESENT
    backend.observation = Observation(True, complete=False, matches=True, receipt=RECEIPT)
    assert port.reconcile(prepared, "b").status is ReconcileStatus.UNKNOWN


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (WriteFailure("before", sent=False), DispatchOutcome.NOT_APPLIED),
        (WriteFailure("refused", sent=True, mutation_possible=False), DispatchOutcome.NOT_APPLIED),
        (WriteFailure("400", sent=True, status=400), DispatchOutcome.NOT_APPLIED),
        (WriteFailure("500", sent=True, status=500), DispatchOutcome.UNKNOWN),
        (WriteFailure("lost", sent=True), DispatchOutcome.UNKNOWN),
        (ValueError("unexpected"), DispatchOutcome.UNKNOWN),
    ],
)
def test_write_failure_classification_is_conservative(
    failure: Exception, expected: DispatchOutcome
) -> None:
    backend = Backend()
    port = CalendarPort(backend)
    prepared = make_create(port)
    backend.write_error = failure
    assert port.dispatch(prepared, "k").outcome is expected
    assert backend.writes == 1
    assert backend.observes == 0


def test_preflight_failure_never_calls_write() -> None:
    backend = Backend()
    port = CalendarPort(backend)
    prepared = make_create(port)
    backend.preflight_error = ValueError("duplicate")
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.NOT_APPLIED
    assert backend.writes == 0


def test_absence_and_deleted_observation_follow_provider_proof() -> None:
    backend = Backend()
    port = CalendarPort(backend)
    create = make_create(port)
    backend.observation = Observation(False, absence_provable=False)
    missing = port.reconcile(create, "k")
    assert missing.status is ReconcileStatus.ABSENT
    assert not missing.proof
    backend.observation = Observation(False, absence_provable=True)
    assert port.reconcile(create, "k").proof
    update = make_update(port)
    backend.observation = Observation(True, unchanged=True, absence_provable=False)
    unchanged = port.reconcile(update, "k")
    assert unchanged.status is ReconcileStatus.ABSENT
    assert not unchanged.proof
    backend.observation = Observation(False)
    assert port.reconcile(update, "k").status is ReconcileStatus.UNKNOWN
    delete = port.prepare(
        CalendarDelete(source=SOURCE, event_ref=HANDLE, expected_etag="v1"), {HANDLE: "remote-id"}
    )
    assert port.reconcile(delete, "k").status is ReconcileStatus.UNKNOWN
    backend.observation = Observation(False, delete_confirmed=True, receipt=RECEIPT)
    assert port.dispatch(delete, "k").outcome is DispatchOutcome.APPLIED
    assert port.reconcile(delete, "k").status is ReconcileStatus.PRESENT
    backend.observation = Observation(
        False, delete_confirmed=True, unchanged=True, absence_provable=True, receipt=RECEIPT
    )
    unchanged_delete = port.reconcile(delete, "k")
    assert unchanged_delete.status is ReconcileStatus.ABSENT
    assert unchanged_delete.proof
    assert backend.writes == 1
