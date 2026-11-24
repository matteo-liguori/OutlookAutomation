"""Calendar and invitation scenarios.

Contract: docs/live-test.md (Scenarios). Calendar writes are never covered by a standing
grant, so each runs only when the owner approved calendar edits (config or option); without
that the scenario is skipped with the reason, never approved by the runner.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from chamberlain.livetest.lab import JSON, Lab, ScenarioSkip, text_of
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


@scenario(
    "calendar.create_update_delete",
    "calendar",
    "Create, update and delete an event",
    Scope.CALENDAR,
    needs=_NEEDS,
)
def create_update_delete(lab: Lab, t: Target) -> None:
    _require_approval(lab)
    title = lab.subject(f"crud {t.tag}")
    start = _soon(lab, 2)
    handle = _create(lab, t.source, _event(title, start))
    before = _view(lab, handle)
    lab.check(before["status"] == "confirmed", "a new event is not confirmed")
    lab.confirmed(
        lab.write(
            {
                "kind": "calendar.update",
                "source": t.source,
                "event_ref": handle,
                "expected_etag": before["etag"],
                "event": _event(title, start, 60, location="Live test room"),
            }
        ),
        "update event",
    )
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
    "Change one instance of a recurring event",
    Scope.CALENDAR,
    needs=_NEEDS,
)
def recurring(lab: Lab, t: Target) -> None:
    raise ScenarioSkip("recurrence not supported by v2 EventFields")


def _wait_find(lab: Lab, source: str, title: str, start: datetime) -> JSON:
    return lab.wait_until(
        f"the invitation reaching {source}",
        lambda: next(iter(_find(lab, source, title, start)), None),
    )


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
        invited_copy = _wait_find(lab, setting.invitee_calendar, title, start)
        seen_on = "calendar"
    else:
        lab.arrival(setting.invitee_mail, title)
    lab.check(bool(seen_on), "the invitation never arrived")
    if invited_copy is not None and setting.invitee_calendar is not None:
        copy = _view(lab, str(invited_copy["handle"]))
        lab.check(
            copy.get("my_response") == "needs_action",
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
            return True if "accepted" in text.lower() else None

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
