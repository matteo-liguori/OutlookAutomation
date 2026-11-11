"""Bounded iCloud CalDAV access for the OutlookAutomation repository.

Credentials are accepted only as JSON on stdin so they do not appear in the
process command line. The PowerShell wrapper supplies a DPAPI-protected secret.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
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
        choices=("probe", "calendars", "events", "create", "update", "delete"),
        required=True,
    )
    parser.add_argument("--calendar")
    parser.add_argument("--uid")
    parser.add_argument("--title")
    parser.add_argument("--location")
    parser.add_argument("--description")
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
