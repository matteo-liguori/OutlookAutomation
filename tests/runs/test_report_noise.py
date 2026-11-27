"""The pass report raises only what needs the owner now (seen live, 2026-11-27).

A source that was only a few minutes behind at report time is disclosed under verification, not
raised as an access problem. Only due reminders ask for acknowledgement; later ones are one line.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from chamberlain.runs import appointments, sections
from chamberlain.runs.appointments import Appointment
from chamberlain.runs.report_repo import SourceFact

NOW = datetime(2026, 11, 27, 14, 10, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


@dataclass
class Ctx:
    clock: Clock = field(default_factory=Clock)


@dataclass
class Facts:
    sources: list[SourceFact]
    zone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/London"))
    gaps: list[str] = field(default_factory=lambda: list[str]())
    unsynced: dict[str, list[str]] = field(default_factory=lambda: dict[str, list[str]]())
    ctx: Any = field(default_factory=Ctx)

    def name(self, source: SourceFact) -> str:
        return source.id


def mail(health: str, gap: tuple[str, str] | None = None, fresh: str | None = None) -> SourceFact:
    return SourceFact(
        "mail:personal",
        "mail",
        "",
        health,
        "2026-11-27T14:10:00Z",
        fresh,
        "2026-11-27T14:05:00Z",
        gap[0] if gap else None,
        gap[1] if gap else None,
        None,
    )


def test_a_mailbox_a_few_minutes_behind_is_disclosed_but_not_an_access_problem() -> None:
    facts = Facts([mail("stale", ("2026-11-27T14:05:00Z", "2026-11-27T14:10:00Z"))])
    assert sections.access_problems(facts) == ""  # type: ignore[arg-type]
    assert facts.gaps == [
        "mail:personal was still catching up when this was written (read at the next check)"
    ]


def test_a_zero_length_gap_is_not_reported() -> None:
    facts = Facts([mail("ok", ("2026-11-27T14:10:00Z", "2026-11-27T14:10:00Z"))])
    assert sections.access_problems(facts) == ""  # type: ignore[arg-type]


def test_a_long_gap_or_an_unreachable_source_is_still_an_access_problem() -> None:
    long_gap = Facts([mail("stale", ("2026-11-27T10:00:00Z", "2026-11-27T14:10:00Z"))])
    text = sections.access_problems(long_gap)  # type: ignore[arg-type]
    assert "has not been read" in text
    assert "last confirmed fresh not yet" in text
    down = Facts([mail("unavailable")])
    assert "mail:personal" in sections.access_problems(down)  # type: ignore[arg-type]


def test_only_due_reminders_ask_for_acknowledgement(monkeypatch: pytest.MonkeyPatch) -> None:
    def item(title: str, start: str, reminder: str, due: bool) -> Appointment:
        return Appointment("act_x", "card_x", "Important dates", title, start, reminder, due)

    monkeypatch.setattr(
        appointments,
        "upcoming",
        lambda ctx: [
            item("Dentist", "2026-11-28T09:00:00Z", "2026-11-27T09:00:00Z", True),
            item("Anniversary in a month", "2027-01-24T00:00:00Z", "2026-12-24T09:00:00Z", False),
            item("Anniversary in a week", "2027-01-24T00:00:00Z", "2027-01-17T09:00:00Z", False),
        ],
    )
    text = sections.important_appointments(Facts([]))  # type: ignore[arg-type]
    lines = text.splitlines()
    assert len(lines) == 2
    assert "Dentist" in lines[0]
    assert "Acknowledgement needed" in lines[0]
    assert lines[1].startswith("- 2 later reminder(s) are scheduled; the next shows Thu 24 Dec")
    assert not lines[1].endswith("..")


def test_with_nothing_due_the_acknowledgement_section_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later = Appointment(
        "act_x",
        "card_x",
        "Important dates",
        "Anniversary.",
        "2027-01-24T00:00:00Z",
        "2026-12-24T09:00:00Z",
        False,
    )
    monkeypatch.setattr(appointments, "upcoming", lambda ctx: [later])
    assert sections.important_appointments(Facts([])) == ""  # type: ignore[arg-type]
