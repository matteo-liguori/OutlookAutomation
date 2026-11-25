"""One real event on two calendars is one event (owner ruling 2026-11-25)."""

from datetime import UTC, datetime, timedelta

from chamberlain.attention.detectors.wave2 import calendar_conflict
from chamberlain.attention.models import CalendarSlot, DetectorInput
from chamberlain.surface.viewer.pages import _merge  # pyright: ignore[reportPrivateUsage]

DAY = datetime(2026, 12, 1, tzinfo=UTC)


def slot(source: str, title: str, hour: float, hours: float, location: str = "") -> CalendarSlot:
    start = DAY + timedelta(hours=hour)
    return CalendarSlot(
        source, f"{source}-{title}", f"uid-{source}-{title}", start,
        start + timedelta(hours=hours), f"fp-{source}-{title}", title=title, location=location,
    )  # fmt: skip


def test_copies_entered_separately_never_clash_but_a_real_overlap_does() -> None:
    slots = (
        # The same flight on the owner's and Imogen's calendars, worded differently.
        slot("calendar:primary", "Flight BA 123 to Rome", 9, 2),
        slot("calendar:icloud-matteo-imogen", "BA123 LHR-FCO", 9, 2.1),
        # The same dinner, entered by each of them.
        slot("calendar:primary", "Dinner with Giulia", 19, 2, "Dishoom Shoreditch"),
        slot("calendar:kcl", "Giulia dinner", 19, 2, "Dishoom, Shoreditch"),
        # A real clash with the dinner.
        slot("calendar:kcl", "Seminar talk", 20, 1),
    )
    found = list(calendar_conflict(DetectorInput(now=DAY, calendar_slots=slots)))
    assert len(found) == 1
    assert "20:00" in found[0].summary


def test_the_viewer_shows_one_event_entered_on_two_calendars_once() -> None:
    def row(source: str, title: str, start: str, end: str, location: str = "") -> dict[str, object]:
        return {
            "source": source, "title": title, "location": location, "all_day": False,
            "first_day": "2026-12-01", "last_day": "2026-12-01", "start_time": start,
            "end_time": end, "start": f"2026-12-01T{start}:00Z",
        }  # fmt: skip

    merged = _merge(
        [
            row("calendar:primary", "Flight BA 123 to Rome", "09:00", "11:00"),
            row("calendar:icloud", "BA123 LHR-FCO", "09:00", "11:05"),
            row("calendar:kcl", "Seminar talk", "09:00", "10:00"),
        ],
        {"calendar:primary": "Primary", "calendar:icloud": "Calendar", "calendar:kcl": "KCL"},
    )
    assert len(merged) == 2
    flight = next(m for m in merged if "BA" in str(m["title"]))
    assert flight["calendars"] == ["Primary", "Calendar"]
