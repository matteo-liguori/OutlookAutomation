"""Google connector behaviour beyond the shared contract (ported from the old harness)."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
from cal_fake_google import FakeGoogleApi
from test_google_contract import build

from chamberlain.connectors.calendar_support.http import HttpReply, TransportFailure
from chamberlain.connectors.fake.world import Fault
from chamberlain.connectors.google_calendar.calendar import GoogleCalendar
from chamberlain.domain.calendar import CalendarQuery, EventFields, EventStatus
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import CalendarCreate, CalendarDelete, CalendarUpdate
from chamberlain.domain.operations import DispatchOutcome, DispatchResult, Prepared, ReconcileStatus
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

SOURCE = "calendar:test"
START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
World = tuple[GoogleCalendar, FakeGoogleApi, ManualClock]


def fields(title: str = "Event", **extra: Any) -> EventFields:
    return EventFields.model_validate(
        {"title": title, "start": START, "end": START + timedelta(minutes=30)} | extra
    )


def window() -> CalendarQuery:
    return CalendarQuery(start=START - timedelta(days=2), end=START + timedelta(days=30))


def item(title: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}} | extra


@pytest.fixture
def world() -> World:
    clock = ManualClock()
    port, api = build(clock)
    return port, api, clock


def create(
    port: GoogleCalendar, uid: str, event: EventFields | None = None
) -> tuple[Prepared, DispatchResult]:
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=event or fields(), uid=uid), {})
    return prepared, port.dispatch(prepared, "k-" + uid)


def edit(
    port: GoogleCalendar,
    locator: str,
    etag: str,
    event: EventFields,
    scope: Literal["occurrence", "series"] | None = None,
) -> Prepared:
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
    intent = CalendarUpdate(
        source=SOURCE, event_ref=handle, expected_etag=etag, event=event, scope=scope
    )
    return port.prepare(intent, {handle: locator})


def locator_of(result: DispatchResult) -> str:
    assert result.receipt
    assert result.receipt.locator
    return result.receipt.locator


def test_p16_create_read_update_round_trip_and_3_related_checks(world: World) -> None:
    # p16_create_read_update_round_trip
    port, api, _ = world
    _, result = create(port, "u1@t", fields("First"))
    assert result.outcome is DispatchOutcome.APPLIED
    assert result.receipt
    assert result.receipt.verification.server_verified
    locator = locator_of(result)
    current = port.read(SOURCE, locator)
    assert (current.uid, current.fields.title) == ("u1@t", "First")
    update = port.dispatch(edit(port, locator, current.etag, fields("Edited")), "k2")
    assert update.outcome is DispatchOutcome.APPLIED
    assert port.read(SOURCE, locator).fields.title == "Edited"
    assert ("PUT", api.calls[-3][1]) in api.calls

    # p16_all_day_events_keep_their_shape
    port, api, _ = world
    day = EventFields(
        title="All day",
        start=datetime(2027, 3, 3, tzinfo=UTC),
        end=datetime(2027, 3, 4, tzinfo=UTC),
        all_day=True,
    )
    _, result = create(port, "d1@t", day)
    assert result.outcome is DispatchOutcome.APPLIED
    locator = locator_of(result)
    assert api.events[locator]["start"] == {"date": "2027-03-03"}
    assert port.read(SOURCE, locator).fields.all_day

    # p16_update_keeps_what_the_fields_do_not_describe: Ported: reminders, colour, the series an
    port, api, _ = world
    locator = api.seed(
        {
            "summary": "Weekly",
            "start": {"dateTime": "2027-03-01T12:00:00", "timeZone": "Europe/London"},
            "end": {"dateTime": "2027-03-01T12:30:00", "timeZone": "Europe/London"},
            "recurrence": ["RRULE:FREQ=DAILY;COUNT=5", "EXDATE:20270302T120000Z"],
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
            "colorId": "5",
        }
    )
    before = port.read(SOURCE, locator)
    renamed = edit(port, locator, before.etag, fields("Renamed"), "series")
    result = port.dispatch(renamed, "k")
    assert result.outcome is DispatchOutcome.APPLIED, result.detail
    after = api.events[locator]
    assert after["summary"] == "Renamed"
    assert after["reminders"]["overrides"][0]["minutes"] == 30
    assert after["colorId"] == "5"
    assert after["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=5", "EXDATE:20270302T120000Z"]
    assert after["start"]["timeZone"] == "Europe/London"

    # p16_occurrences_and_invitations_from_others_are_refused_without_sending
    port, api, _ = world
    guest = api.seed(
        item("Invite", "2027-03-01T10:00:00Z", "2027-03-01T11:00:00Z")
        | {"attendees": [{"email": "other@example.test"}]}
    )
    instance = api.seed(
        item("Occurrence", "2027-03-02T10:00:00Z", "2027-03-02T11:00:00Z")
        | {"recurringEventId": "master"}
    )
    sent = len([c for c in api.calls if c[0] != "GET"])
    for locator in (guest, instance):
        etag = port.read(SOURCE, locator).etag
        handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
        delete = CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=etag)
        with pytest.raises(ChamberlainError) as raised:
            port.prepare(delete, {handle: locator})
        assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert len([c for c in api.calls if c[0] != "GET"]) == sent
    assert port.read(SOURCE, guest).fields.attendees == ("other@example.test",)


def test_p16_private_appointment_round_trips_without_guests_or_notifications(world: World) -> None:
    port, api, _ = world
    _, created = create(port, "private@t", fields("Private", visibility="private"))
    assert created.outcome is DispatchOutcome.APPLIED
    locator = locator_of(created)
    current = port.read(SOURCE, locator)
    updated = port.dispatch(edit(port, locator, current.etag, fields("Updated")), "private-k2")
    assert updated.outcome is DispatchOutcome.APPLIED
    assert port.read(SOURCE, locator).fields.visibility == "private"
    assert len(api.bodies) == 2
    assert all(body["visibility"] == "private" and "attendees" not in body for body in api.bodies)
    assert all("sendUpdates" not in params for method, _, params in api.params if method != "GET")


def test_p11_an_ambiguous_google_uid_never_selects_one_of_its_events(world: World) -> None:
    port, api, _ = world
    uid = "duplicate@t"
    for _ in range(2):
        api.seed(item("Event", "2027-03-01T10:00:00Z", "2027-03-01T10:30:00Z", iCalUID=uid))
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid=uid), {})
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.NOT_APPLIED
    assert port.reconcile(prepared, "k").status is ReconcileStatus.UNKNOWN
    assert all(method == "GET" for method, _ in api.calls)


def test_w10_server_errors_may_have_landed_client_errors_and_1_related_checks(
    world: World,
) -> None:
    # w10_server_errors_may_have_landed_client_errors_did_not
    port, api, _ = world
    cases = [(500, True), (503, True), (408, True), (429, True)]
    for status, may_have_happened in [*cases, (400, False), (403, False), (409, False)]:
        api.fail_next["POST"] = status
        _, result = create(port, f"e{status}@t")
        expected = DispatchOutcome.UNKNOWN if may_have_happened else DispatchOutcome.NOT_APPLIED
        assert result.outcome is expected, status
        assert not api.live(f"e{status}@t"), status

    # w10_an_unreadable_verification_after_a_write_is_unknown: Ported (2026-11-24): the pre-writ
    port, api, _ = world
    real = api.send
    gets = {"n": 0}

    def flaky(method: str, url: str, **kwargs: Any) -> HttpReply:
        if method == "GET":
            gets["n"] += 1
            if gets["n"] > 1:
                return HttpReply(503)
        return real(method, url, **kwargs)

    api.send = flaky  # type: ignore[method-assign]
    prepared, result = create(port, "v1@t")
    assert result.outcome is DispatchOutcome.UNKNOWN
    api.send = real  # type: ignore[method-assign]
    assert port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT


def test_p10_a_connection_that_never_opened_is_not_applied(world: World) -> None:
    port, api, _ = world

    def refuse(*args: Any, **kwargs: Any) -> HttpReply:
        raise TransportFailure("refused", sent=False)

    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid="n@t"), {})
    api.send = refuse  # type: ignore[method-assign]
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.NOT_APPLIED


def test_p11_reconcile_of_an_update_settles_from_the_read_back(world: World) -> None:
    port, api, _ = world
    locator = api.seed(item("Old", "2027-03-01T10:00:00Z", "2027-03-01T10:30:00Z"))
    prepared = edit(port, locator, port.read(SOURCE, locator).etag, fields("New"))
    before = port.reconcile(prepared, "k")
    assert (before.status, before.proof) == (ReconcileStatus.ABSENT, True)
    api.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    settled = port.reconcile(prepared, "k")
    assert settled.status is ReconcileStatus.PRESENT
    assert settled.receipt
    assert settled.receipt.locator == locator
    api.events[locator]["summary"] = "Somebody else"
    api.events[locator]["etag"] = '"other"'
    assert port.reconcile(prepared, "k").status is ReconcileStatus.UNKNOWN


def test_p4_listing_is_paged_with_an_opaque_cursor_and_hides_cancelled(world: World) -> None:
    port, api, _ = world
    for index in range(5):
        start = START + timedelta(hours=index)
        end = start + timedelta(minutes=30)
        api.seed(item(f"E{index}", start.isoformat(), end.isoformat()))
    api.seed(item("Gone", START.isoformat(), START.isoformat()) | {"status": "cancelled"})
    query = window().model_copy(update={"limit": 2})
    first = port.events(SOURCE, query)
    assert [e.fields.title for e in first.items] == ["E0", "E1"]
    assert not first.complete
    assert first.cursor
    titles = [e.fields.title for e in first.items]
    page = first
    while page.cursor:
        page = port.events(SOURCE, query, page.cursor)
        titles += [e.fields.title for e in page.items]
    assert titles == ["E0", "E1", "E2", "E3", "E4"]
    assert page.complete
    with pytest.raises(ChamberlainError) as bad:
        port.events(SOURCE, query, "!!!")
    assert bad.value.code is ErrorCode.REQUEST_BAD_CURSOR


def test_p18_odd_remote_events_read_without_raising(world: World) -> None:
    port, api, _ = world
    locator = api.seed(
        item("", "2027-03-01T10:00:00Z", "2027-03-01T09:00:00Z")
        | {"description": "x" * 9000, "location": "y" * 900}
    )
    event = port.read(SOURCE, locator)
    assert event.fields.title == "(no title)"
    assert len(event.fields.description) == 4000
    assert event.fields.end >= event.fields.start


def test_p5_health_distinguishes_sign_in_from_outage(world: World) -> None:
    port, api, _ = world
    assert port.health(SOURCE).state.value == "ok"
    real = api.send
    api.send = lambda *a, **k: HttpReply(401)  # type: ignore[method-assign]
    assert port.health(SOURCE).state.value == "sign_in_needed"

    def down(*args: Any, **kwargs: Any) -> HttpReply:
        raise TransportFailure("refused", sent=False)

    api.send = down  # type: ignore[method-assign]
    assert port.health(SOURCE).state.value == "unavailable"
    api.send = real  # type: ignore[method-assign]
    with pytest.raises(ChamberlainError) as unknown:
        port.capabilities("calendar:other")
    assert unknown.value.code is ErrorCode.SOURCE_NOT_CONFIGURED


def test_p16_a_cancelled_event_is_reported_cancelled(world: World) -> None:
    port, api, _ = world
    locator = api.seed(
        item("X", "2027-03-01T10:00:00Z", "2027-03-01T11:00:00Z") | {"status": "cancelled"}
    )
    assert port.read(SOURCE, locator).status is EventStatus.CANCELLED


def test_delete_reconcile_requires_a_changed_revision_for_a_google_tombstone(world: World) -> None:
    port, api, _ = world
    locator = api.seed(item("X", "2027-03-01T10:00:00Z", "2027-03-01T11:00:00Z"))
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
    prepared = port.prepare(
        CalendarDelete(
            source=SOURCE, event_ref=handle, expected_etag=port.read(SOURCE, locator).etag
        ),
        {handle: locator},
    )
    api.arm(Fault.LOSE_BEFORE_APPLY)
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    api.events[locator]["status"] = "cancelled"
    unchanged = port.reconcile(prepared, "k")
    assert unchanged.status is ReconcileStatus.ABSENT
    assert unchanged.proof

    api.events[locator]["etag"] = '"cancelled"'
    changed = port.reconcile(prepared, "k")
    assert changed.status is ReconcileStatus.PRESENT
    assert [method for method, _ in api.calls if method == "DELETE"] == ["DELETE"]


def test_p27_an_all_day_series_with_an_until_is_verified_by_date(world: World) -> None:
    """An all-day series ends on a date: a time of day in `until` must not fail the read-back."""
    port, api, _ = world
    event = fields(
        "Holiday",
        start=datetime(2027, 3, 1, tzinfo=UTC),
        end=datetime(2027, 3, 2, tzinfo=UTC),
        all_day=True,
        recurrence={
            "frequency": "daily",
            "until": datetime(2027, 3, 3, 23, 59, 59, tzinfo=UTC),
            "zone": "Europe/London",
        },
    )
    prepared, result = create(port, "allday-series@t", event)
    assert result.outcome is DispatchOutcome.APPLIED, result.detail
    assert port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT
    assert api.live("allday-series@t")[0]["recurrence"] == ["RRULE:FREQ=DAILY;UNTIL=20270303"]


def test_google_request_build_failure_is_not_sent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chamberlain.connectors.calendar_support.port import WriteFailure
    from chamberlain.connectors.google_calendar import calendar as module

    port, api, _ = world
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid="build-fail"), {})
    calls = list(api.calls)

    def fail(plan: Prepared) -> Any:
        raise ValueError("synthetic")

    monkeypatch.setattr(module, "payload", fail)
    with pytest.raises(WriteFailure) as raised:
        port.write(prepared)
    assert not raised.value.sent
    assert api.calls == calls
