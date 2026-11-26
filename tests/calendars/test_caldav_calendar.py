"""CalDAV connector behaviour beyond the shared contract, against real Radicale."""

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from cal_radicale import RadicaleServer
from cal_rigs import CalDavRig, FaultyTransport
from test_caldav_contract import basic, build

from chamberlain.connectors.caldav.calendar import CalDavCalendar, resource_name
from chamberlain.connectors.caldav.config import CalDavBinding, CalDavConfig
from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.calendar_support.http import UrllibTransport
from chamberlain.connectors.calendar_support.port import payload
from chamberlain.domain.calendar import CalendarQuery, EventFields, EventStatus
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import CalendarCreate, CalendarDelete, CalendarUpdate
from chamberlain.domain.operations import DispatchOutcome, Prepared, ReconcileStatus
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from support.rigs import Fault

SOURCE = "calendar:test"
START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
World = tuple[CalDavCalendar, CalDavRig]


def fields(title: str = "Event", **extra: object) -> EventFields:
    return EventFields.model_validate(
        {"title": title, "start": START, "end": START + timedelta(minutes=30)} | extra
    )


def window(days: int = 30) -> CalendarQuery:
    return CalendarQuery(start=START - timedelta(days=2), end=START + timedelta(days=days))


def raw(title: str, uid: str, body: str = "", start: str = "20270301T100000Z") -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//other client//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20270101T000000Z\r\nDTSTART:{start}\r\n"
        f"DTEND:{start.replace('10', '11', 1)}\r\nSUMMARY:{title}\r\n{body}"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


@pytest.fixture
def world(radicale: RadicaleServer) -> World:
    clock = ManualClock()
    port, transport, collection = build(radicale, clock)
    return port, CalDavRig(port, transport, clock, collection, basic())


def put(rig: CalDavRig, name: str, text: str) -> None:
    reply = rig.transport.inner.send(
        "PUT",
        rig.collection + name,
        headers={"Authorization": basic(), "Content-Type": "text/calendar"},
        body=text.encode(),
        timeout=10,
    )
    assert reply.status in (201, 204), reply.body


def prepare_edit(
    port: CalDavCalendar,
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


def test_p16_a_stale_update_is_refused_by_the_server_not_and_1_related_checks(
    world: World,
) -> None:
    # p16_a_stale_update_is_refused_by_the_server_not_just_by_prepare
    port, rig = world
    locator = rig.seed(fields("Contract event"))
    old = port.read(SOURCE, locator)
    prepared = prepare_edit(port, locator, old.etag, fields("Mine"))
    current = rig.transport.inner.send(
        "GET", rig.collection + locator, headers={"Authorization": basic()}, body=None, timeout=10
    ).body.decode()
    put(rig, locator, current.replace("SUMMARY:Contract event", "SUMMARY:Theirs"))  # another client
    result = port.dispatch(prepared, "k")
    assert result.outcome is DispatchOutcome.NOT_APPLIED
    assert "412" in result.detail
    assert port.read(SOURCE, locator).fields.title == "Theirs"
    assert port.reconcile(prepared, "k").status is ReconcileStatus.UNKNOWN

    # p16_a_create_that_exists_under_another_name_is_found_by_uid
    port, rig = world
    uid = "elsewhere@t"
    put(rig, "other-client-name.ics", raw("From another client", uid))
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid=uid), {})
    settled = port.reconcile(prepared, "k")
    assert settled.status is ReconcileStatus.PRESENT
    assert settled.receipt
    assert settled.receipt.locator == "other-client-name.ics"


def test_p16_p4_update_keeps_alarms_recurrence_and_time_zone_and_1_related_checks(
    world: World,
) -> None:
    # p16_update_keeps_alarms_recurrence_and_time_zone
    port, rig = world
    series = raw(
        "Daily",
        "series@t",
        "RRULE:FREQ=DAILY;COUNT=3\r\nEXDATE:20270302T100000Z\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\n"
        "DESCRIPTION:x\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\nX-CUSTOM:keep-me\r\n",
    )
    put(rig, "series.ics", series)
    before = port.read(SOURCE, "series.ics")
    new = fields("Renamed", start=before.fields.start, end=before.fields.end)
    result = port.dispatch(prepare_edit(port, "series.ics", before.etag, new, "series"), "k")
    assert result.outcome is DispatchOutcome.APPLIED, result.detail
    text = rig.transport.inner.send(
        "GET",
        rig.collection + "series.ics",
        headers={"Authorization": basic()},
        body=None,
        timeout=10,
    ).body.decode()
    for kept in (
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE:20270302T100000Z",
        "BEGIN:VALARM",
        "X-CUSTOM:keep-me",
    ):
        assert kept in text
    assert "SUMMARY:Renamed" in text
    assert "DTSTART:20270301T100000Z" in text

    # p4_a_recurring_series_is_listed_as_occurrences_and_written_only_with_its_scope
    port, rig = world
    put(rig, "series.ics", raw("Daily", "series@t", "RRULE:FREQ=DAILY;COUNT=3\r\n"))
    page = port.events(SOURCE, window())
    assert page.complete
    assert [e.locator for e in page.items] == [
        "series.ics#20270301T100000Z",
        "series.ics#20270302T100000Z",
        "series.ics#20270303T100000Z",
    ]
    occurrence = port.read(SOURCE, "series.ics#20270302T100000Z")
    assert occurrence.fields.start == START + timedelta(days=1)
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, occurrence.locator)
    delete = CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=occurrence.etag)
    with pytest.raises(ChamberlainError) as raised:
        port.prepare(delete, {handle: occurrence.locator})
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_p16_someone_elses_invitation_is_never_changed_or_cancelled(world: World) -> None:
    port, rig = world
    guests = "ATTENDEE:mailto:other@example.test\r\nORGANIZER:mailto:me@example.test\r\n"
    put(rig, "invite.ics", raw("Invite", "invite@t", guests))
    current = port.read(SOURCE, "invite.ics")
    assert current.fields.attendees == ("other@example.test",)
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "invite.ics")
    for intent in (
        CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=current.etag),
        CalendarUpdate(
            source=SOURCE, event_ref=handle, expected_etag=current.etag, event=fields("x")
        ),
    ):
        with pytest.raises(ChamberlainError) as raised:
            port.prepare(intent, {handle: "invite.ics"})
        assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
        assert raised.value.detail == "invitee-copy"
    assert not rig.transport.sent or all(m not in ("PUT", "DELETE") for m, _ in rig.transport.sent)


def test_a_timed_out_delete_does_not_confirm_an_unchanged_cancelled_event(world: World) -> None:
    port, rig = world
    locator = "already-cancelled.ics"
    put(rig, locator, raw("Cancelled earlier", "cancelled@t", "STATUS:CANCELLED\r\n"))
    before = port.read(SOURCE, locator)
    assert before.status is EventStatus.CANCELLED
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
    prepared = port.prepare(
        CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=before.etag),
        {handle: locator},
    )
    assert rig.inject(Fault.LOSE_BEFORE_APPLY)
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    settled = port.reconcile(prepared, "k")
    assert settled.status is ReconcileStatus.ABSENT
    assert settled.proof
    assert port.read(SOURCE, locator).etag == before.etag
    assert not any(method == "DELETE" for method, _ in rig.transport.sent)


def test_p4_pages_and_text_filter(world: World) -> None:
    port, rig = world
    for index in range(5):
        start = START + timedelta(hours=index)
        rig.seed(fields(f"Item {index}", start=start, end=start + timedelta(minutes=30)))
    query = window().model_copy(update={"limit": 2})
    seen: list[str] = []
    page = port.events(SOURCE, query)
    seen += [e.fields.title for e in page.items]
    while page.cursor:
        page = port.events(SOURCE, query, page.cursor)
        seen += [e.fields.title for e in page.items]
    assert seen == [f"Item {i}" for i in range(5)]
    assert page.complete
    only = port.events(SOURCE, window().model_copy(update={"text": "item 3"}))
    assert [e.fields.title for e in only.items] == ["Item 3"]
    with pytest.raises(ChamberlainError) as bad:
        port.events(SOURCE, query, "nonsense")
    assert bad.value.code is ErrorCode.REQUEST_BAD_CURSOR


def test_p18_p16_odd_events_read_without_raising_and_1_related_checks(world: World) -> None:
    # p18_odd_events_read_without_raising
    port, rig = world
    odd = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nBEGIN:VEVENT\r\nUID:odd@t\r\n"
        "DTSTAMP:20270101T000000Z\r\nDTSTART;TZID=Europe/London:20270327T120000\r\n"
        "DTEND;TZID=Europe/London:20270327T090000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    put(rig, "odd.ics", odd)
    event = port.read(SOURCE, "odd.ics")
    assert event.fields.title == "(no title)"
    assert event.fields.start == datetime(2027, 3, 27, 12, 0, tzinfo=UTC)
    assert event.fields.end >= event.fields.start

    # p16_all_day_events_round_trip
    port, _ = world
    day = EventFields(
        title="Holiday",
        start=datetime(2027, 3, 3, tzinfo=UTC),
        end=datetime(2027, 3, 4, tzinfo=UTC),
        all_day=True,
    )
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=day, uid="allday@t"), {})
    assert "DTSTART;VALUE=DATE:20270303" in str(payload(prepared)["ics"])
    result = port.dispatch(prepared, "k")
    assert result.outcome is DispatchOutcome.APPLIED
    assert result.receipt
    assert result.receipt.locator
    assert port.read(SOURCE, result.receipt.locator).fields == day


def test_p5_health_and_failures_by_cause(radicale: RadicaleServer) -> None:
    clock = ManualClock()
    collection = radicale.new_calendar()

    def make(url: str, password: str) -> CalDavCalendar:
        config = CalDavConfig(
            bindings={SOURCE: CalDavBinding(url=url, username="alice", credential="c")}
        )
        return CalDavCalendar(config, lambda binding: password, clock, UrllibTransport())

    assert make(collection, "radicale-test-password").health(SOURCE).state.value == "ok"
    wrong = make(collection, "wrong")
    assert wrong.health(SOURCE).state.value == "sign_in_needed"
    with pytest.raises(ChamberlainError) as refused:
        wrong.events(SOURCE, window())
    assert refused.value.code is ErrorCode.SOURCE_SIGN_IN_NEEDED
    nobody = make("http://127.0.0.1:9/alice/x/", "p")
    assert nobody.health(SOURCE).state.value == "unavailable"
    with pytest.raises(ChamberlainError) as down:
        nobody.read(SOURCE, "a.ics")
    assert down.value.code is ErrorCode.SOURCE_UNAVAILABLE
    prepared = nobody.prepare(CalendarCreate(source=SOURCE, event=fields(), uid="z@t"), {})
    assert nobody.dispatch(prepared, "k").outcome is DispatchOutcome.NOT_APPLIED


def test_s17_a_password_is_never_sent_over_plain_http_to_another_host() -> None:
    config = CalDavConfig(
        bindings={
            SOURCE: CalDavBinding(
                url="http://calendar.example.com/x/", username="a", credential="c"
            )
        }
    )
    with pytest.raises(ChamberlainError) as raised:
        CalDavCalendar(config, lambda binding: "p", ManualClock())
    assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_p10_a_server_error_after_sending_is_unknown(world: World) -> None:
    port, rig = world
    real = rig.transport.inner.send

    class Failing(FaultyTransport):
        pass

    def broken(method: str, url: str, **kwargs: object) -> object:
        if method == "PUT":
            from chamberlain.connectors.calendar_support.http import HttpReply

            return HttpReply(503)
        return real(method, url, **kwargs)  # type: ignore[arg-type]

    rig.transport.inner.send = broken  # type: ignore[method-assign]
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid="five@t"), {})
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    rig.transport.inner.send = real  # type: ignore[method-assign]
    assert port.reconcile(prepared, "k").status is ReconcileStatus.ABSENT


def test_p27_an_all_day_series_with_an_until_is_verified_by_date(world: World) -> None:
    port, _ = world
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
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=event, uid="allday@t"), {})
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.APPLIED
    assert port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT
    assert len(port.events(SOURCE, window()).items) == 3


@pytest.mark.parametrize(
    "required",
    [{"visibility": "private"}, {"recurrence": {"frequency": "daily", "count": 3, "zone": "UTC"}}],
)
def test_p27_create_reconcile_checks_requested_privacy_or_rule(
    world: World, required: dict[str, object]
) -> None:
    port, rig = world
    uid = "provider-lost-a-field@t"
    wanted = EventFields.model_validate(fields().model_dump() | required)
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=wanted, uid=uid), {})
    put(rig, resource_name(uid), ics.build_new(uid, fields(), rig.clock.now()))

    assert port.reconcile(prepared, "k").status is ReconcileStatus.UNKNOWN

    put(rig, resource_name(uid), ics.build_new(uid, wanted, rig.clock.now()))
    found = port.reconcile(prepared, "k")
    assert found.status is ReconcileStatus.PRESENT
    assert found.receipt is not None
    assert found.receipt.verification.server_verified
