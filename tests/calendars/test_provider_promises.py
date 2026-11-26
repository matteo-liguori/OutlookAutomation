"""Promises both calendar connectors make, once each: Google (fake API) and CalDAV (Radicale)."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
from cal_radicale import RadicaleServer
from cal_rigs import CalDavRig
from test_caldav_contract import basic
from test_caldav_contract import build as build_caldav
from test_caldav_invitations import received
from test_google_contract import build as build_google
from test_google_invitations import seed_received

from chamberlain.connectors.caldav import calendar as caldav_module
from chamberlain.connectors.calendar_support.port import WriteFailure
from chamberlain.connectors.google_calendar import calendar as google_module
from chamberlain.connectors.google_calendar.calendar import event_id_for
from chamberlain.domain.calendar import CalendarQuery, EventFields, Response
from chamberlain.domain.intents import CalendarCreate
from chamberlain.domain.operations import DispatchOutcome, Prepared, ReconcileStatus
from chamberlain.ports.clock import ManualClock

SOURCE = "calendar:test"
START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
GUEST = "guest@example.test"
EVENT = EventFields(title="Event", start=START, end=START + timedelta(minutes=30))


@dataclass
class Provider:
    port: Any
    module: ModuleType
    wire: Callable[[], list[Any]]  # every request sent to the provider so far
    series_stored: Callable[[], None]  # the all-day series as the provider holds it
    receive: Callable[[], str]  # seed a received invitation; returns its locator
    guests_dropped: Callable[[], Any]  # context manager: reads come back without the guests
    received_guests: tuple[str, ...]


@pytest.fixture(params=["google", "caldav"])
def provider(request: pytest.FixtureRequest) -> Provider:
    clock = ManualClock()
    if request.param == "google":
        port, api = build_google(clock)

        @contextmanager
        def dropped() -> Iterator[None]:
            real, key, stash = api.send, event_id_for("lossy@t"), {}

            def send(method: str, url: str, **kwargs: Any) -> Any:
                reply = real(method, url, **kwargs)
                if method == "POST" and "attendees" in api.events.get(key, {}):
                    stash["attendees"] = api.events[key].pop("attendees")
                return reply

            api.send = send  # type: ignore[method-assign]
            yield
            api.send = real  # type: ignore[method-assign]
            api.events[key].update(stash)

        def stored() -> None:
            assert api.live("allday@t")[0]["recurrence"] == ["RRULE:FREQ=DAILY;UNTIL=20270303"]

        return Provider(
            port,
            google_module,
            lambda: list(api.calls),
            stored,
            lambda: seed_received(api),
            dropped,
            ("boss@example.test", "peer@example.test"),
        )
    radicale: RadicaleServer = request.getfixturevalue("radicale")
    port, transport, collection = build_caldav(radicale, clock)
    rig = CalDavRig(port, transport, clock, collection, basic())

    @contextmanager
    def stripped() -> Iterator[None]:
        real = rig.transport.inner.send

        def send(method: str, url: str, **kwargs: Any) -> Any:
            reply = real(method, url, **kwargs)
            if method == "GET" and reply.body:
                lines = [
                    line
                    for line in reply.body.decode().replace("\r\n ", "").split("\r\n")
                    if not line.startswith(("ATTENDEE", "ORGANIZER"))
                ]
                return type(reply)(reply.status, reply.headers, "\r\n".join(lines).encode())
            return reply

        rig.transport.inner.send = send  # type: ignore[method-assign]
        yield
        rig.transport.inner.send = real  # type: ignore[method-assign]

    def window() -> CalendarQuery:
        return CalendarQuery(start=START - timedelta(days=2), end=START + timedelta(days=30))

    def held() -> None:
        assert len(port.events(SOURCE, window()).items) == 3

    def receive() -> str:
        received(rig, "boss.ics")
        return "boss.ics"

    return Provider(
        port,
        caldav_module,
        lambda: list(rig.transport.sent),
        held,
        receive,
        stripped,
        ("boss@example.test",),
    )


def create(provider: Provider, uid: str, event: EventFields) -> Prepared:
    return provider.port.prepare(CalendarCreate(source=SOURCE, event=event, uid=uid), {})


def test_p27_an_all_day_series_with_an_until_is_verified_by_date(provider: Provider) -> None:
    """An all-day series ends on a date: a time of day in `until` must not fail the read-back."""
    event = EventFields.model_validate(
        {
            "title": "Holiday",
            "start": datetime(2027, 3, 1, tzinfo=UTC),
            "end": datetime(2027, 3, 2, tzinfo=UTC),
            "all_day": True,
            "recurrence": {
                "frequency": "daily",
                "until": datetime(2027, 3, 3, 23, 59, 59, tzinfo=UTC),
                "zone": "Europe/London",
            },
        }
    )
    prepared = create(provider, "allday@t", event)
    result = provider.port.dispatch(prepared, "k")
    assert result.outcome is DispatchOutcome.APPLIED, result.detail
    assert provider.port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT
    provider.series_stored()


def test_a_request_that_cannot_be_built_is_not_sent(
    provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = create(provider, "build-fail", EVENT)
    sent = provider.wire()

    def fail(plan: Prepared) -> object:
        raise ValueError("synthetic")

    monkeypatch.setattr(provider.module, "payload", fail)
    with pytest.raises(WriteFailure) as raised:
        provider.port.write(prepared)
    assert not raised.value.sent
    assert provider.wire() == sent


def test_w36_a_read_back_without_the_guests_is_not_verified(provider: Provider) -> None:
    event = EVENT.model_copy(update={"attendees": (GUEST,)})
    prepared = create(provider, "lossy@t", event)
    with provider.guests_dropped():
        assert provider.port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
        assert provider.port.reconcile(prepared, "k").status is ReconcileStatus.UNKNOWN
    assert provider.port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT


def test_w33_a_received_invitation_reads_with_its_organiser_and_my_answer(
    provider: Provider,
) -> None:
    event = provider.port.read(SOURCE, provider.receive())
    assert event.organiser == "boss@example.test"
    assert event.my_response is Response.NEEDS_ACTION
    assert event.fields.attendees == provider.received_guests
