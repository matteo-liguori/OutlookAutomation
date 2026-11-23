"""Bounded iCloud CalDAV access for the OutlookAutomation repository.

Credentials are accepted only as JSON on stdin so they do not appear in the
process command line. The PowerShell wrapper supplies a DPAPI-protected secret.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import uuid4

import caldav
from caldav.elements import cdav, dav
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _read_credentials() -> tuple[str, str]:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # pragma: no cover - exact parser message varies
        raise ValueError("Credentials must be supplied as JSON on stdin.") from exc
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    if not username or not password:
        raise ValueError("Both username and password are required.")
    return username, password


def _calendar_name(calendar: Any) -> str:
    try:
        return str(calendar.get_display_name() or "")
    except Exception:
        return ""


def _component_value(component: Any, key: str) -> Any:
    value = component.get(key)
    if value is None:
        return None
    if hasattr(value, "dt"):
        return value.dt
    return str(value)


def _event_component(resource: Any) -> Any:
    component = resource.icalendar_component
    if getattr(component, "name", "") == "VEVENT":
        return component
    for child in component.walk("VEVENT"):
        return child
    raise ValueError("CalDAV resource did not contain a VEVENT.")


def _event_record(resource: Any, calendar_name: str) -> dict[str, Any]:
    component = _event_component(resource)
    start = _component_value(component, "DTSTART")
    end = _component_value(component, "DTEND")
    return {
        "calendar": calendar_name,
        "uid": _component_value(component, "UID"),
        "title": _component_value(component, "SUMMARY") or "",
        "start": start,
        "end": end,
        "allDay": isinstance(start, date) and not isinstance(start, datetime),
        "location": _component_value(component, "LOCATION") or "",
        "status": _component_value(component, "STATUS") or "",
        "recurrenceId": _component_value(component, "RECURRENCE-ID"),
        "url": str(resource.url),
    }


def _is_recurring(component: Any) -> bool:
    return component.get("RRULE") is not None or component.get("RECURRENCE-ID") is not None


def _annual_month_plan(payload, uid, keep_month, effective):
    calendar = ICalendar.from_ical(payload)
    events = calendar.walk("VEVENT")
    if len(events) != 1:
        raise ValueError("Expected a single recurrence master without exceptions.")
    event = events[0]
    if str(event.get("UID")) != uid or any(
        field in event for field in ("RECURRENCE-ID", "RDATE", "EXDATE", "EXRULE", "ATTENDEE", "ORGANIZER")
    ):
        raise ValueError("The series has exceptions, scheduling participants, or a mismatched UID.")
    start = _component_value(event, "DTSTART")
    end = _component_value(event, "DTEND")
    if (type(start) is not date or type(end) is not date or (end - start).days != 1
            or start.day > 28 or start.month != keep_month or effective <= start):
        raise ValueError("Use a one-day all-day series starting in the month to keep, with a fixed day up to 28.")
    rule = event.get("RRULE")
    if (not isinstance(rule, dict) or rule.keys() - {"FREQ", "INTERVAL", "BYMONTHDAY"}
            or rule.get("FREQ") != ["MONTHLY"] or rule.get("BYMONTHDAY", [start.day]) != [start.day]):
        raise ValueError("Use a simple open-ended monthly rule with a fixed day.")
    interval, = rule.get("INTERVAL", [1])
    if interval not in (1, 2, 3, 4, 6):
        raise ValueError("The original monthly interval must divide the year and repeat more than once a year.")

    def occurrence(month_index):
        year, month = divmod(month_index, 12)
        return date(year, month + 1, start.day)

    first = start.year * 12 + start.month - 1
    limit = effective.year * 12 + effective.month - 1
    preserved = [
        day for index in range(first, limit + 1, interval)
        if (day := occurrence(index)) < effective and day.month != keep_month
    ]
    next_year = effective.year + (effective > date(effective.year, keep_month, start.day))
    next_kept = date(next_year, keep_month, start.day)
    next_removed = next(
        day for index in range(first + ((limit - first) // interval) * interval, limit + 25, interval)
        if (day := occurrence(index)) >= effective and day.month != keep_month
    )
    del event["RRULE"]
    event.add("rrule", {"freq": "yearly", "bymonth": keep_month, "bymonthday": start.day})
    if preserved:
        event.add("rdate", preserved)
    return calendar, {
        "uid": uid, "title": str(event["SUMMARY"]), "effective": effective,
        "nextKept": next_kept, "nextRemoved": next_removed, "preservedDates": preserved,
    }


def _change_annual_month(client, calendar, uid, keep_month, effective, expected_revision):
    url = str(calendar.url.join(f"{uid}.ics"))
    current = client.request(url, "GET", "")
    if current.status != 200:
        raise ValueError(f"Could not read the exact series (HTTP {current.status}).")
    raw = current.raw.encode("utf-8") if isinstance(current.raw, str) else current.raw
    revision = sha256(raw + f"\0{url}\0{keep_month}\0{effective}".encode()).hexdigest()
    if expected_revision is not None and expected_revision != revision:
        raise ValueError("The series changed since preview. Read and preview it again.")
    proposed, plan = _annual_month_plan(raw, uid, keep_month, effective)
    if expected_revision is None:
        return dict(plan, preview=True, revision=revision)

    etag = current.headers.get("ETag", "")
    if not (etag.startswith('"') and etag.endswith('"')):
        raise ValueError("iCloud did not provide a strong revision tag for the conditional update.")
    body = proposed.to_ical()
    response = client.request(url, "PUT", body,
                              headers={"If-Match": etag, "Content-Type": "text/calendar; charset=utf-8"})
    if response.status not in (200, 204):
        raise ValueError(f"iCloud rejected the conditional series update (HTTP {response.status}).")
    saved = client.request(url, "GET", "")
    if saved.status != 200:
        raise ValueError("iCloud accepted the write, but the series could not be read back.")
    expected, = ICalendar.from_ical(body).walk("VEVENT")
    actual = ICalendar.from_ical(saved.raw).walk("VEVENT")
    def recurrence_dates(event):
        dates = event.get("RDATE", [])
        return sorted(item.dt for group in (dates if isinstance(dates, list) else [dates]) for item in group.dts)

    if (len(actual) != 1
            or any(_component_value(actual[0], field) != _component_value(expected, field)
                   for field in ("UID", "SUMMARY", "DTSTART", "DTEND"))
            or actual[0].get("RRULE") != expected["RRULE"]
            or recurrence_dates(actual[0]) != plan["preservedDates"]):
        raise ValueError("iCloud accepted the write, but readback did not match the planned series.")
    return dict(plan, preview=False, verified=True)


def _find_calendar(calendars: list[Any], name: str) -> Any:
    matches = [
        calendar for calendar in calendars if _calendar_name(calendar).casefold() == name.casefold()
    ]
    if not matches:
        available = ", ".join(sorted(_calendar_name(calendar) for calendar in calendars))
        raise ValueError(f"Calendar not found: {name}. Available: {available}")
    if len(matches) != 1:
        raise ValueError(f"Calendar name is ambiguous: {name}")
    return matches[0]


def _set_or_remove(component: Any, key: str, value: str | None) -> None:
    if value is None:
        return
    if key in component:
        del component[key]
    if value:
        component.add(key, value)


def _exact_duplicate(
    calendar: Any, title: str, start: datetime, end: datetime
) -> dict[str, Any] | None:
    resources = calendar.search(
        start=start, end=end, event=True, server_expand=True, split_expanded=True
    )
    for resource in resources:
        component = _event_component(resource)
        existing_title = str(component.get("SUMMARY") or "").strip().casefold()
        existing_start = _component_value(component, "DTSTART")
        existing_end = _component_value(component, "DTEND")
        if (
            existing_title == title.strip().casefold()
            and existing_start == start
            and existing_end == end
        ):
            return _event_record(resource, _calendar_name(calendar))
    return None


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Dates must include an explicit UTC offset or Z suffix.")
    return parsed


def _output(payload: Any, pretty: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            default=_json_default,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=("probe", "calendars", "events", "create", "update", "delete", "annual-month"),
        required=True,
    )
    parser.add_argument("--calendar")
    parser.add_argument("--uid")
    parser.add_argument("--title")
    parser.add_argument("--location")
    parser.add_argument("--description")
    parser.add_argument("--keep-month", type=int, choices=range(1, 13))
    parser.add_argument("--effective-date", type=date.fromisoformat)
    parser.add_argument("--expected-revision")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--all-day", action="store_true")
    parser.add_argument("--allow-duplicate", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    try:
        username, password = _read_credentials()
        with caldav.DAVClient(
            url=ICLOUD_CALDAV_URL,
            username=username,
            password=password,
            timeout=args.timeout,
            require_tls=True,
        ) as client:
            principal = client.principal()
            if args.action == "probe":
                _output(
                    {"success": True, "account": username, "service": "iCloud CalDAV"}, args.pretty
                )
                return 0

            calendars = principal.calendars()
            calendar_records = [
                {
                    "name": _calendar_name(calendar),
                    "url": str(calendar.url),
                }
                for calendar in calendars
            ]
            calendar_records.sort(key=lambda item: item["name"].casefold())
            if args.action == "calendars":
                _output(
                    {
                        "success": True,
                        "count": len(calendar_records),
                        "calendars": calendar_records,
                    },
                    args.pretty,
                )
                return 0

            start = None
            end = None
            if args.action in ("events", "create"):
                if not args.start or not args.end:
                    raise ValueError(f"{args.action.title()} requires --start and --end.")
                start = _parse_datetime(args.start)
                end = _parse_datetime(args.end)
                if end <= start:
                    raise ValueError("--end must be later than --start.")
                if (end - start).total_seconds() > 31 * 86400:
                    raise ValueError("The event window is limited to 31 days.")

            selected = calendars
            if args.calendar:
                selected = [_find_calendar(calendars, args.calendar)]

            if args.action == "annual-month":
                if (not args.calendar or not args.uid or not args.keep_month or not args.effective_date
                        or args.effective_date < date.today()):
                    raise ValueError("Supply a calendar, UID, month to keep, and current or future effective date.")
                if any(c in args.uid for c in "/\\?#%"):
                    raise ValueError("Use a single calendar resource UID.")
                plan = _change_annual_month(client, selected[0], args.uid, args.keep_month,
                                            args.effective_date, args.expected_revision)
                _output(dict(plan, success=True), args.pretty)
                return 0

            if args.action == "create":
                if not args.calendar or not args.title:
                    raise ValueError("Create requires --calendar and --title.")
                calendar = selected[0]
                duplicate = _exact_duplicate(calendar, args.title, start, end)
                if duplicate and not args.allow_duplicate:
                    _output(
                        {"success": False, "blocked": "exactDuplicate", "match": duplicate},
                        args.pretty,
                    )
                    return 3

                payload = ICalendar()
                payload.add("prodid", "-//Codex OutlookAutomation//iCloud CalDAV//EN")
                payload.add("version", "2.0")
                event = IEvent()
                event.add("uid", str(uuid4()))
                event.add("dtstamp", datetime.now(timezone.utc))
                event.add("summary", args.title)
                if args.all_day:
                    event.add("dtstart", start.date())
                    event.add("dtend", end.date())
                else:
                    event.add("dtstart", start.astimezone(timezone.utc))
                    event.add("dtend", end.astimezone(timezone.utc))
                if args.location:
                    event.add("location", args.location)
                if args.description:
                    event.add("description", args.description)
                payload.add_component(event)
                saved = calendar.save_event(payload.to_ical())
                saved.load()
                _output(
                    {
                        "success": True,
                        "action": "created",
                        "event": _event_record(saved, _calendar_name(calendar)),
                    },
                    args.pretty,
                )
                return 0

            if args.action in ("update", "delete"):
                if not args.calendar or not args.uid:
                    raise ValueError(f"{args.action.title()} requires --calendar and --uid.")
                calendar = selected[0]
                # iCloud rejects UID-filter calendar-query REPORT requests with
                # HTTP 412. Its event resources use the UID as the .ics name,
                # so resolve the resource directly instead.
                resource = calendar.event_by_url(calendar.url.join(f"{args.uid}.ics"))
                component = _event_component(resource)
                if _is_recurring(component):
                    raise ValueError(
                        "Recurring event mutation is blocked until occurrence scope is explicitly implemented."
                    )

                before = _event_record(resource, _calendar_name(calendar))
                if args.action == "delete":
                    try:
                        resource.delete()
                    except Exception as exc:
                        # iCloud may require an explicit wildcard precondition
                        # even after the resource has just been loaded.
                        if "412" not in str(exc):
                            raise
                        etag = getattr(resource, "etag", None) or resource.get_property(
                            dav.GetEtag()
                        )
                        if not etag:
                            raise
                        schedule_tag = getattr(
                            resource, "schedule_tag", None
                        ) or resource.get_property(cdav.ScheduleTag())
                        headers = {"If-Match": str(etag)}
                        if schedule_tag:
                            headers["If-Schedule-Tag-Match"] = str(schedule_tag)
                        response = resource.client.request(
                            str(resource.url),
                            "DELETE",
                            "",
                            headers=headers,
                        )
                        if response.status not in (200, 204):
                            response._generate_exception()
                    _output({"success": True, "action": "deleted", "event": before}, args.pretty)
                    return 0

                if args.title is not None:
                    _set_or_remove(component, "SUMMARY", args.title)
                _set_or_remove(component, "LOCATION", args.location)
                _set_or_remove(component, "DESCRIPTION", args.description)
                if args.start or args.end:
                    raise ValueError(
                        "Update of event timing is not yet enabled; use title/location/description only."
                    )
                resource.save(no_create=True, only_this_recurrence=False)
                resource.load()
                _output(
                    {
                        "success": True,
                        "action": "updated",
                        "before": before,
                        "event": _event_record(resource, _calendar_name(calendar)),
                    },
                    args.pretty,
                )
                return 0

            events: list[dict[str, Any]] = []
            failures: list[dict[str, str]] = []
            for calendar in selected:
                name = _calendar_name(calendar)
                try:
                    resources = calendar.search(
                        start=start,
                        end=end,
                        event=True,
                        server_expand=True,
                        split_expanded=True,
                    )
                    events.extend(_event_record(resource, name) for resource in resources)
                except Exception as exc:
                    failures.append({"calendar": name, "error": str(exc)})

            events.sort(
                key=lambda item: (
                    str(item["start"]),
                    item["calendar"].casefold(),
                    item["title"].casefold(),
                )
            )
            _output(
                {
                    "success": not failures,
                    "window": {"start": start, "end": end},
                    "calendarCount": len(selected),
                    "eventCount": len(events),
                    "events": events,
                    "failures": failures,
                },
                args.pretty,
            )
            return 0 if not failures else 2
    except Exception as exc:
        _output({"success": False, "errorType": type(exc).__name__, "error": str(exc)}, args.pretty)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
