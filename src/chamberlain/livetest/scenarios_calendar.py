"""Calendar and invitation scenarios.

Contract: docs/live-test.md (Scenarios). Calendar writes are never covered by a standing
grant, so each runs only when the owner approved calendar edits (config or option); without
that the scenario is skipped with the reason, never approved by the runner.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from chamberlain.livetest.lab import JSON, Lab, ScenarioFailure, ScenarioSkip, text_of
from chamberlain.livetest.registry import Scope, Target, scenario

LONDON = ZoneInfo("Europe/London")
STAMP = "%Y-%m-%dT%H:%M:%SZ"
_NEEDS = ("calendar.read", "calendar.write")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(STAMP)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, STAMP).replace(tzinfo=UTC)


def _require_approval(lab: Lab) -> None:
    if not lab.approve_edits:
        raise ScenarioSkip(
            "calendar writes need the owner's approval: set "
            "live_test.approve_calendar_edits or pass --approve-calendar-edits"
        )


def _soon(lab: Lab, days: int, hour: int = 10) -> datetime:
    day = lab.env.clock.now() + timedelta(days=days)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0)


def _event(title: str, start: datetime, minutes: int = 30, **more: object) -> JSON:
    fields: JSON = {
        "title": title,
        "start": _stamp(start),
        "end": _stamp(start + timedelta(minutes=minutes)),
    }
    fields.update(more)
    return fields


def _create(lab: Lab, source: str, fields: JSON) -> str:
    """Create an event and return its `cev_` handle."""
    lab.defer(
        f"remove marked events in {source}", lambda: lab.remove_events(source, fields["title"])
    )
    view = lab.confirmed(
        lab.write({"kind": "calendar.create", "source": source, "event": fields}), "create event"
    )
    return _handle_of(lab, source, fields, view)


def _find(lab: Lab, source: str, title: str, around: datetime) -> list[JSON]:
    found = lab.ok(
        "find",
        scope="calendar",
        source=source,
        query=title,
        since=_stamp(around - timedelta(days=1)),
        until=_stamp(around + timedelta(days=2)),
    )
    return [hit for hit in found.get("hits", []) if title in text_of(hit)]


def _handle_of(lab: Lab, source: str, fields: JSON, view: JSON) -> str:
    if view.get("result_ref"):
        return str(view["result_ref"])
    start = datetime.strptime(str(fields["start"]), STAMP).replace(tzinfo=UTC)
    hits = _find(lab, source, str(fields["title"]), start)
    lab.check(len(hits) == 1, f"expected one new event, found {len(hits)}")
    return str(hits[0]["handle"])


def _view(lab: Lab, handle: str) -> JSON:
    view: JSON = lab.ok("open", handle=handle)["view"]
    return view


def _delete(lab: Lab, source: str, handle: str) -> None:
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.delete",
                "source": source,
                "event_ref": handle,
                "expected_etag": _view(lab, handle)["etag"],
            }
        ),
        "delete event",
    )


def _gone(lab: Lab, source: str, title: str, start: datetime) -> None:
    left = [h for h in _find(lab, source, title, start) if h.get("summary") != "cancelled"]
    lab.check(not left, "the deleted event still shows as live")


def _check_private(lab: Lab, handle: str) -> None:
    lab.used_probe("private calendar event with no guests")
    source, locator = lab.app.context.services.refs.locate(handle)
    event = lab.env.probe.read_event(source, locator)
    lab.check(event.fields.visibility == "private", "the calendar did not keep private visibility")
    lab.check(
        not (event.fields.attendees or event.participants or event.organiser),
        "the test event has guests or is an invitation",
    )


@scenario(
    "calendar.create_update_delete",
    "calendar",
    "Create, update and delete a private event without guests",
    Scope.CALENDAR,
    needs=_NEEDS,
)
def create_update_delete(lab: Lab, t: Target) -> None:
    _require_approval(lab)
    title = lab.subject(f"crud {t.tag}")
    start = _soon(lab, 2)
    handle = _create(lab, t.source, _event(title, start, visibility="private"))
    _check_private(lab, handle)
    before = _view(lab, handle)
    lab.check(before["status"] == "confirmed", "a new event is not confirmed")
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.update",
                "source": t.source,
                "event_ref": handle,
                "expected_etag": before["etag"],
                "event": _event(title, start, 60, location="Live test room", visibility="private"),
            }
        ),
        "update event",
    )
    _check_private(lab, handle)
    after = _view(lab, handle)
    lab.check(after["etag"] != before["etag"], "the etag did not change after an update")
    text = text_of(lab.ok("open", handle=handle, part="body"))
    lab.check("Live test room" in text, "the updated location is not on the event")
    lab.check(after["end"] != before["end"], "the updated end time was not kept")
    _delete(lab, t.source, handle)
    _gone(lab, t.source, title, start)


@scenario(
    "calendar.all_day",
    "calendar",
    "An all-day event keeps its dates",
    Scope.CALENDAR,
    needs=_NEEDS,
)
def all_day(lab: Lab, t: Target) -> None:
    _require_approval(lab)
    title = lab.subject(f"all_day {t.tag}")
    day = _soon(lab, 3, hour=0)
    fields = _event(title, day, 24 * 60, all_day=True)
    handle = _create(lab, t.source, fields)
    view = _view(lab, handle)
    lab.check(view["all_day"] is True, "the event is not all-day on read-back")
    lab.check(
        view["start"] == fields["start"],
        f"all-day start moved from {fields['start']} to {view['start']}",
    )
    _delete(lab, t.source, handle)
    _gone(lab, t.source, title, day)


def _next_london_change(after: datetime) -> datetime:
    """Midnight (UTC date) of the first day on which London's UTC offset changes."""
    day = after.replace(hour=12, minute=0, second=0, microsecond=0)
    for _ in range(400):
        today = day.astimezone(LONDON).utcoffset()
        tomorrow = (day + timedelta(days=1)).astimezone(LONDON).utcoffset()
        if today != tomorrow:
            return (day + timedelta(days=1)).replace(hour=0)
        day += timedelta(days=1)
    raise ScenarioSkip("no daylight-saving change within a year")


@scenario(
    "calendar.timezone",
    "calendar",
    "Nine o'clock London keeps its instant across a clock change",
    Scope.CALENDAR,
    needs=_NEEDS,
)
def timezone_case(lab: Lab, t: Target) -> None:
    _require_approval(lab)
    change = _next_london_change(lab.env.clock.now()).astimezone(LONDON)
    handles: list[tuple[str, str, datetime]] = []
    for label, day in (("before", change - timedelta(days=1)), ("after", change)):
        wall = datetime(day.year, day.month, day.day, 9, 0, tzinfo=LONDON)
        title = lab.subject(f"timezone_{label} {t.tag}")
        handles.append((title, _create(lab, t.source, _event(title, wall, 60)), wall))
    for _title, handle, wall in handles:
        view = _view(lab, handle)
        expected = _stamp(wall)
        lab.check(
            view["start"] == expected,
            f"09:00 London on {wall.date()} came back as {view['start']}, not {expected}",
        )
    first = _parse(_view(lab, handles[0][1])["start"])
    second = _parse(_view(lab, handles[1][1])["start"])
    hours = (second - first).total_seconds() / 3600
    lab.check(
        hours in (23.0, 25.0),
        f"nine o'clock on two days either side of the change are {hours} hours apart, not 23 or 25",
    )
    for title, handle, wall in handles:
        _delete(lab, t.source, handle)
        _gone(lab, t.source, title, wall)


@scenario(
    "calendar.recurring",
    "calendar",
    "A repeating event: change one date, cancel one, change the series, delete it",
    Scope.CALENDAR,
    needs=(*_NEEDS, "calendar.recurrence", "calendar.occurrence"),
)
def recurring(lab: Lab, t: Target) -> None:
    _require_approval(lab)
    title = lab.subject(f"series {t.tag}")
    start = _soon(lab, 2)
    lab.defer(f"remove marked events in {t.source}", lambda: lab.remove_events(t.source, title))
    rule: JSON = {"frequency": "daily", "count": 3}
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.create",
                "source": t.source,
                "event": _event(title, start, recurrence=rule),
            }
        ),
        "create series",
    )

    def dates() -> list[JSON]:
        found = lab.ok(
            "find",
            scope="calendar",
            source=t.source,
            query=title,
            since=_stamp(start - timedelta(days=1)),
            until=_stamp(start + timedelta(days=4)),
        )
        return [h for h in found.get("hits", []) if title in text_of(h)]

    def live() -> list[JSON]:
        return [h for h in dates() if not str(h.get("summary", "")).startswith("cancelled")]

    seen = lab.wait_until(
        "the three dates of the series showing", lambda: dates() if len(dates()) == 3 else None
    )
    lab.check(
        all(str(h.get("summary", "")).endswith("repeats") for h in seen),
        "a date of the series is not marked as repeating",
    )
    first = _view(lab, str(seen[0]["handle"]))
    lab.check(first.get("repeats") == "occurrence", "a date is not an occurrence of a series")
    series = str(first["series"])

    # One date changed: its location, nothing else.
    second = _view(lab, str(seen[1]["handle"]))
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.update",
                "source": t.source,
                "event_ref": seen[1]["handle"],
                "expected_etag": second["etag"],
                "scope": "occurrence",
                "event": _event(title, _parse(second["start"]), location="Live test room"),
            }
        ),
        "change one date",
    )
    lab.check(
        "Live test room" in text_of(lab.ok("open", handle=seen[1]["handle"], part="body")),
        "the changed date did not keep its new location",
    )
    lab.check(
        "Live test room" not in text_of(lab.ok("open", handle=seen[0]["handle"], part="body")),
        "changing one date changed another",
    )

    # One date cancelled.
    third = _view(lab, str(seen[2]["handle"]))
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.delete",
                "source": t.source,
                "event_ref": seen[2]["handle"],
                "expected_etag": third["etag"],
                "scope": "occurrence",
            }
        ),
        "cancel one date",
    )
    lab.check(len(live()) == 2, f"{len(live())} dates are live after one was cancelled, not 2")

    # The whole series: every date that was not changed on its own.
    master = _view(lab, series)
    lab.check(master.get("repeats") == "series", "the series handle is not the series")
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.update",
                "source": t.source,
                "event_ref": series,
                "expected_etag": master["etag"],
                "scope": "series",
                "event": {
                    "title": title,
                    "start": master["start"],
                    "end": master["end"],
                    "location": "Series room",
                },
            }
        ),
        "change the series",
    )
    lab.check(
        "Series room" in text_of(lab.ok("open", handle=seen[0]["handle"], part="body")),
        "the series change did not reach an unchanged date",
    )

    master = _view(lab, series)
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.delete",
                "source": t.source,
                "event_ref": series,
                "expected_etag": master["etag"],
                "scope": "series",
            }
        ),
        "delete the series",
    )
    lab.check(not live(), "a date of the series is still live after the series was deleted")


def _wait_find(lab: Lab, source: str, title: str, start: datetime) -> JSON:
    return lab.wait_until(
        f"the invitation reaching {source}",
        lambda: next(iter(_find(lab, source, title, start)), None),
    )


def _invited[T](
    lab: Lab, invitee_mail: str | None, calendar: str, title: str, wait: Callable[[], T]
) -> T:
    """Wait for the invitation on the invitee's calendar; if it never comes, say whether it
    arrived as mail (then the calendar did not add it: a setting, not a harness fault)."""
    try:
        return wait()
    except ScenarioFailure as missing:
        if invitee_mail is None or not lab.hits(invitee_mail, title):
            raise
        raise ScenarioFailure(
            f"the invitation arrived as mail in {invitee_mail}, but {calendar} did not add it "
            "to the calendar; on Google, check Settings > Event settings > Add invitations to "
            "my calendar"
        ) from missing


@scenario(
    "calendar.invite_flow",
    "calendar",
    "Invite an owned account, answer from its side, then cancel",
    Scope.CALENDAR,
    needs=(*_NEEDS, "calendar.invite"),
)
def invite_flow(lab: Lab, t: Target) -> None:
    _require_approval(lab)
    setting = lab.config.calendars[t.source]
    if setting.invitee_mail is None:
        raise ScenarioSkip("live_test.calendars has no invitee_mail for this calendar")
    invitee = lab.config.address_of(setting.invitee_mail)
    title = lab.subject(f"invite {t.tag}")
    start = _soon(lab, 4)
    lab.defer(
        "remove invitation mail",
        lambda: lab.remove_mail(str(setting.invitee_mail), title),
    )
    organiser = _create(lab, t.source, _event(title, start, attendees=[invitee]))
    seen_on = "mailbox"
    invited_copy: JSON | None = None
    if setting.invitee_calendar is not None:
        lab.defer(
            "remove invitee-side events",
            lambda: lab.remove_events(str(setting.invitee_calendar), title),
        )
        invited_copy = _invited(
            lab,
            setting.invitee_mail,
            setting.invitee_calendar,
            title,
            lambda: _wait_find(lab, str(setting.invitee_calendar), title, start),
        )
        seen_on = "calendar"
    else:
        lab.arrival(setting.invitee_mail, title)
    lab.check(bool(seen_on), "the invitation never arrived")
    if invited_copy is not None and setting.invitee_calendar is not None:
        copy = _view(lab, str(invited_copy["handle"]))
        lab.check(
            copy.get("my_response", "needs_action") == "needs_action",
            f"the invitee's copy starts as {copy.get('my_response')}, not needs_action",
        )
        lab.confirmed(
            lab.write(
                {
                    "kind": "calendar.respond",
                    "source": setting.invitee_calendar,
                    "event_ref": invited_copy["handle"],
                    "expected_etag": copy["etag"],
                    "response": "accepted",
                }
            ),
            "respond to the invitation",
        )

        def organiser_sees_answer() -> bool | None:
            text = text_of(lab.ok("open", handle=organiser, part="body"))
            if "accepted" in text.lower():
                return True
            lab.used_probe("the organiser's per-guest answers")
            source, locator = lab.app.context.services.refs.locate(organiser)
            answers = lab.env.probe.answers(source, locator)
            return True if answers.get(invitee) == "accepted" else None

        lab.wait_until("the organiser's copy showing the answer", organiser_sees_answer)
    _delete(lab, t.source, organiser)
    if invited_copy is not None:
        handle = str(invited_copy["handle"])

        def cancelled() -> bool | None:
            read = lab.call("open", handle=handle)
            if not read.ok:
                return True
            return True if read.data["view"].get("status") == "cancelled" else None

        lab.wait_until("the cancellation reaching the invitee", cancelled)


@scenario(
    "calendar.recurring_invite",
    "calendar",
    "Invite an owned account to a repeating event, change one date, cancel the series",
    Scope.CALENDAR,
    needs=(*_NEEDS, "calendar.invite", "calendar.recurrence", "calendar.occurrence"),
)
def recurring_invite(lab: Lab, t: Target) -> None:
    _require_approval(lab)
    setting = lab.config.calendars[t.source]
    if setting.invitee_mail is None:
        raise ScenarioSkip("live_test.calendars has no invitee_mail for this calendar")
    invitee = lab.config.address_of(setting.invitee_mail)
    title = lab.subject(f"series invite {t.tag}")
    start = _soon(lab, 5)
    lab.defer("remove invitation mail", lambda: lab.remove_mail(str(setting.invitee_mail), title))
    lab.defer(f"remove marked events in {t.source}", lambda: lab.remove_events(t.source, title))
    rule: JSON = {"frequency": "daily", "count": 3}
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.create",
                "source": t.source,
                "event": _event(title, start, attendees=[invitee], recurrence=rule),
            }
        ),
        "create a repeating event with a guest",
    )

    def dates(source: str) -> list[JSON]:
        found = lab.ok(
            "find",
            scope="calendar",
            source=source,
            query=title,
            since=_stamp(start - timedelta(days=1)),
            until=_stamp(start + timedelta(days=4)),
        )
        return [
            h
            for h in found.get("hits", [])
            if title in text_of(h) and not str(h.get("summary", "")).startswith("cancelled")
        ]

    mine = lab.wait_until(
        "the three dates showing", lambda: dates(t.source) if len(dates(t.source)) == 3 else None
    )
    if setting.invitee_calendar is None:
        lab.arrival(setting.invitee_mail, title)
    else:
        invited = str(setting.invitee_calendar)
        lab.defer("remove invitee-side events", lambda: lab.remove_events(invited, title))
        _invited(
            lab,
            setting.invitee_mail,
            invited,
            title,
            lambda: lab.wait_until(
                "every date of the invitation reaching the invitee",
                lambda: True if len(dates(invited)) == 3 else None,
            ),
        )

    # One date changed: the guest stays invited to it and to the others.
    second = _view(lab, str(mine[1]["handle"]))
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.update",
                "source": t.source,
                "event_ref": mine[1]["handle"],
                "expected_etag": second["etag"],
                "scope": "occurrence",
                "event": _event(
                    title, _parse(second["start"]), location="Live test room", attendees=[invitee]
                ),
            }
        ),
        "change one date of the invitation",
    )
    if setting.invitee_calendar is not None:
        invited = str(setting.invitee_calendar)
        lab.wait_until(
            "the invitee keeping all three dates after one changed",
            lambda: True if len(dates(invited)) == 3 else None,
        )

    series = str(_view(lab, str(mine[0]["handle"]))["series"])
    master = _view(lab, series)
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.delete",
                "source": t.source,
                "event_ref": series,
                "expected_etag": master["etag"],
                "scope": "series",
            }
        ),
        "cancel the repeating invitation",
    )
    if setting.invitee_calendar is not None:
        invited = str(setting.invitee_calendar)
        lab.wait_until(
            "the cancellation reaching every date of the invitee",
            lambda: True if not dates(invited) else None,
        )
