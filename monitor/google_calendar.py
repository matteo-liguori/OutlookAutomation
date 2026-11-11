from .configuration import zone as configured_zone
"""Validate bounded connector exports; optionally record complete harness sources."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from .model import CAL, dumps, now, stamp
from .profile import PROFILE, DEFAULT_PROVIDERS

SOURCES = {s for s in CAL if {**DEFAULT_PROVIDERS, **PROFILE.get('calendar_providers', {})}.get(s) == 'google'}


def capture_time(value):
    if not isinstance(value, str):
        raise ValueError("Missing capture timestamp")
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if point.tzinfo is None:
        raise ValueError("Capture timestamp needs an explicit offset")
    return point


def check_config(config):
    if (
        set(config) != SOURCES
        or any(not isinstance(v, str) or not v for v in config.values())
        or ("calendar:primary" in config and config["calendar:primary"] != "primary")
        or any(v == 'primary' for k,v in config.items() if k != 'calendar:primary')
    ):
        raise ValueError("Google calendar IDs must match enabled profile sources")


def prepare(config, day=None, at=None):
    check_config(config)
    day = (
        date.fromisoformat(day)
        if day
        else datetime.fromisoformat(stamp(at or now())).astimezone(configured_zone()).date()
    )
    start = datetime.combine(day, time(), configured_zone())
    end = datetime.combine(day + timedelta(days=4), time(), configured_zone())
    return dict(
        start=start.isoformat(),
        end=end.isoformat(),
        timezone=configured_zone().key,
        days=[(day + timedelta(days=n)).isoformat() for n in range(4)],
        sources=[dict(source=s, calendar_id=config[s]) for s in sorted(SOURCES)],
        allowed_sources=sorted(SOURCES),
    )


def event_time(value, default_zone=None):
    default_zone=default_zone or configured_zone().key
    # The connector exposes ISO strings; native Google exports use date objects.
    # Normalize here without rewriting the raw evidence saved by ingest.
    if isinstance(value, str):
        value = {"date" if len(value) == 10 else "dateTime": value}
    if not isinstance(value, dict):
        raise ValueError("Event start/end must be ISO strings or Google date objects")
    if value.get("date"):
        return (
            datetime.combine(
                date.fromisoformat(value["date"]),
                time(),
                ZoneInfo(value.get("timeZone") or default_zone),
            ),
            True,
        )
    if not value.get("dateTime"):
        raise ValueError("Event has no dateTime/date")
    point = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    if point.tzinfo is None:
        if not value.get("timeZone"):
            raise ValueError("Timed event is missing its timezone")
        zone = ZoneInfo(value["timeZone"])
        options = set()
        for fold in (0, 1):
            candidate = point.replace(tzinfo=zone, fold=fold)
            if candidate.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None) == point:
                options.add(candidate.utcoffset())
        if len(options) != 1:
            raise ValueError("Ambiguous or nonexistent event time needs an explicit offset")
        point = point.replace(tzinfo=zone)
    return point, False


def normalize_event(raw, start, end):
    """Normalize one event or cancellation, preserving explicit timezone semantics."""
    if not isinstance(raw, dict):
        raise ValueError("Event must be an object")
    if not raw.get("id"):
        raise ValueError("Missing stable event ID")
    if raw.get("status") == "cancelled":
        cancelled = {
            "id": raw["id"],
            "title": raw.get("summary") or "(Cancelled event)",
        }
        if raw.get("start") and raw.get("end"):
            s, all_day = event_time(raw["start"])
            e, end_all_day = event_time(raw["end"])
            if all_day != end_all_day or e <= s:
                raise ValueError("Invalid cancelled event duration")
            if s < end and e > start:
                cancelled.update(
                    start=s.isoformat(),
                    end=e.isoformat(),
                    all_day=all_day,
                    location=raw.get("location") or "",
                )
        return cancelled, True
    if raw.get("recurrence"):
        raise ValueError("Unexpanded recurring series; current occurrence coverage unverified")
    s, all_day = event_time(raw.get("start"))
    e, end_all_day = event_time(raw.get("end"))
    if all_day != end_all_day or e <= s:
        raise ValueError("Invalid event duration or mixed all-day/timed dates")
    if not (s < end and e > start):
        raise ValueError("Out-of-window event rejected")
    normalized = dict(
        id=raw["id"],
        title=raw.get("summary") or "(Untitled event)",
        start=s.isoformat(),
        end=e.isoformat(),
        location=raw.get("location") or "",
        all_day=all_day,
    )
    return normalized, False


def normalize(payload, config):
    check_config(config)
    start = datetime.fromisoformat(payload["start"])
    end = datetime.fromisoformat(payload["end"])
    if start.tzinfo is None or end.tzinfo is None or payload.get("timezone") != configured_zone().key:
        raise ValueError("Collection needs explicit London window offsets")
    expected = prepare(config, start.astimezone(configured_zone()).date().isoformat())
    if start != datetime.fromisoformat(expected["start"]) or end != datetime.fromisoformat(
        expected["end"]
    ):
        raise ValueError("Expected exactly four complete London calendar days")
    records = payload.get("sources", [])
    if len(records) != len(SOURCES) or {r["source"] for r in records} != SOURCES:
        raise ValueError("Both authoritative Google sources must have explicit results")
    begun = capture_time(payload.get("started_at"))
    finished = capture_time(payload.get("completed_at"))
    if finished < begun:
        raise ValueError("Collection completion precedes its start")
    results = []
    for record in records:
        source = record["source"]
        if record["calendar_id"] != config[source]:
            raise ValueError("Calendar identity mismatch")
        errors = []
        events = {}
        cancellations = []
        token = None
        seen = set()
        pages = record.get("pages", [])
        if record.get("error"):
            errors.append(str(record["error"])[:600])
        if not pages:
            errors.append("No successful calendar response; access unavailable")
        if len(pages) > 10:
            errors.append("Page cap exceeded")
        last_capture = begun
        for index, page in enumerate(pages[:10]):
            if index and token is None:
                errors.append("Extra page after terminal cursor")
            if page.get("requested_page_token") != token:
                errors.append("Missing or reordered page")
            if token in seen:
                errors.append("Repeated page token")
            seen.add(token)
            if not isinstance(page.get("events"), list) or len(page.get("events", [])) > 100:
                errors.append("Missing or oversized events array")
                continue
            try:
                captured = capture_time(page.get("fetched_at"))
                if not last_capture <= captured <= finished:
                    raise ValueError("Page capture outside collection or out of order")
                last_capture = captured
            except ValueError as exc:
                errors.append(str(exc))
            for raw in page["events"]:
                try:
                    event, cancelled = normalize_event(raw, start, end)
                    if cancelled:
                        cancellations.append(event)
                    else:
                        if event["id"] in events and events[event["id"]] != event:
                            raise ValueError("Conflicting copies of an event")
                        events[event["id"]] = event
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append(str(exc))
            token = page.get("next_page_token") or None
        if token:
            errors.append("Unconsumed page cursor")
        if not record.get("complete"):
            errors.append("Collection explicitly incomplete")
        if set(events) & {e["id"] for e in cancellations}:
            errors.append("Conflicting active and cancelled event ID")
        complete = not errors
        access = (
            "unavailable"
            if not pages
            or record.get("error_code") in {"authentication-unavailable", "permission-denied"}
            else "available"
        )
        results.append(
            dict(
                kind="source",
                source=source,
                calendar_id=config[source],
                status="current" if complete else "unavailable",
                complete=complete,
                days=expected["days"],
                events=sorted(events.values(), key=lambda e: e["start"]),
                explicit_cancellations=cancellations,
                errors=errors,
                access=access,
                started_at=payload.get("started_at"),
                completed_at=payload.get("completed_at"),
                provenance="command",
            )
        )
    return {"sources": results, "fully_verified": all(s["complete"] for s in results)}


def ingest(h, rid, payload, config):
    result = normalize(payload, config)
    run = h.run(rid)
    day = datetime.fromisoformat(run["started_at"]).astimezone(configured_zone()).date().isoformat()
    if prepare(config, day)["start"] != payload["start"]:
        raise ValueError("Google window does not match this run")
    if capture_time(payload["started_at"]) < capture_time(run["started_at"]):
        raise ValueError("Collection preceded this run; collect again")
    if capture_time(payload["completed_at"]) > capture_time(now()):
        raise ValueError("Collection completion is in the future")
    evidence = h.put_evidence(
        dict(
            source="google-calendar",
            kind="google-connector-export",
            body=dumps(payload),
            captured_at=payload["completed_at"],
            provenance="command",
        )
    )["id"]
    for source in result["sources"]:
        source["evidence_ids"] = [evidence]
    h.record_runs(rid, result["sources"])
    return result
