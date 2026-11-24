"""Pure grant rules (05-grants.md G-1, G-6, G-8, G-12; 04-writes.md W-22)."""

import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from chamberlain.domain.grant_rules import (
    anniversary,
    check_config_requirement,
    next_review,
    requirement_for,
    scope_mismatch,
    stricter,
    suspend_at,
)
from chamberlain.domain.grants import Requirement, StandingScope
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    IntentKind,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
    WriteIntent,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.testing.write_rig import CALENDAR, MAIL, WriteRig
from chamberlain.writes.models import WritePolicy

LONDON = ZoneInfo("Europe/London")
UTC_ZONE = ZoneInfo("UTC")
R = Requirement
SEND = MailSend(source=MAIL, to=("friend@example.org",), subject="s", body="b")


def at(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


# --- G-6: anniversaries -----------------------------------------------------------------


def test_g6_anniversary_short_months_clamp_without_drifting() -> None:
    """Old: test_anniversary_short_months_and_london_dst."""
    granted = at(2027, 1, 31, 10, 0)
    assert anniversary(granted, 1, UTC_ZONE) == at(2027, 2, 28, 10, 0)
    assert anniversary(granted, 2, UTC_ZONE) == at(2027, 3, 31, 10, 0)
    assert anniversary(granted, 3, UTC_ZONE) == at(2027, 4, 30, 10, 0)
    leap = at(2027, 12, 31, 10, 0)
    assert anniversary(leap, 2, UTC_ZONE) == at(2028, 2, 29, 10, 0)


def test_g6_anniversary_keeps_the_london_wall_clock_across_daylight_saving() -> None:
    granted = at(2026, 1, 15, 9, 30)
    assert anniversary(granted, 2, LONDON) == at(2026, 3, 15, 9, 30)
    assert anniversary(granted, 3, LONDON) == at(2026, 4, 15, 8, 30)
    assert anniversary(granted, 10, LONDON) == at(2026, 11, 15, 9, 30)
    summer = at(2026, 7, 10, 8, 0)
    assert anniversary(summer, 4, LONDON) == at(2026, 11, 10, 9, 0)


def test_g6_an_unknown_grant_date_is_due_now_not_at_the_migration_date() -> None:
    """Old: test_unknown_grant_due_now_not_migration_date."""
    now = at(2026, 5, 5, 12, 0)
    assert next_review(None, now, UTC_ZONE) == now


def test_g7_the_next_review_is_the_first_anniversary_after_now_even_when_overdue() -> None:
    granted = at(2026, 1, 10, 9, 0)
    assert next_review(granted, at(2026, 1, 10, 9, 0), UTC_ZONE) == at(2026, 2, 10, 9, 0)
    assert next_review(granted, at(2026, 8, 1, 0, 0), UTC_ZONE) == at(2026, 8, 10, 9, 0)
    assert next_review(granted, at(2026, 8, 10, 9, 0), UTC_ZONE) == at(2026, 9, 10, 9, 0)


def test_g8_an_unreviewed_grant_is_suspended_fourteen_days_after_review_is_due() -> None:
    assert suspend_at(at(2026, 3, 1)) == at(2026, 3, 1) + timedelta(days=14)


@given(
    granted=st.datetimes(
        min_value=datetime(2025, 1, 1, tzinfo=UTC), max_value=datetime(2030, 1, 1, tzinfo=UTC)
    ),
    months=st.integers(min_value=1, max_value=40),
    zone=st.sampled_from(("UTC", "Europe/London", "America/New_York")),
)
def test_g6_property_the_schedule_never_drifts_from_the_grant_date(
    granted: datetime, months: int, zone: str
) -> None:
    tz = ZoneInfo(zone)
    start = granted
    local = start.astimezone(tz)
    got = anniversary(start, months, tz).astimezone(tz)
    index = local.year * 12 + local.month - 1 + months
    year, month = index // 12, index % 12 + 1
    assert (got.year, got.month) == (year, month)
    assert got.day == min(local.day, calendar.monthrange(year, month)[1])
    assert anniversary(start, months + 1, tz) > anniversary(start, months, tz)
    gap = local.hour * 60 + local.minute
    if not 60 <= gap <= 180:  # wall times near a daylight-saving gap may legitimately shift
        assert (got.hour, got.minute, got.second) == (local.hour, local.minute, local.second)


# --- G-12: requirement selection --------------------------------------------------------


def need(
    intent: WriteIntent,
    known: frozenset[str] = frozenset(),
    *,
    send_known: R = R.AGENT_EVIDENCE,
    other: R = R.AGENT_EVIDENCE,
    existing_attendees: bool = False,
) -> R:
    return requirement_for(
        intent, known, send_known=send_known, other=other, existing_attendees=existing_attendees
    )


def test_g12_requirement_table() -> None:
    known = frozenset({"friend@example.org"})
    assert need(SEND, known) is R.AGENT_EVIDENCE
    assert need(SEND, frozenset()) is R.HUMAN_CONFIRMATION
    stranger = SEND.model_copy(update={"to": ("friend@example.org", "new@example.org")})
    assert need(stranger, known) is R.HUMAN_CONFIRMATION
    assert need(SEND, known, send_known=R.HUMAN_CONFIRMATION) is R.HUMAN_CONFIRMATION
    mark = MailMarkRead(source=MAIL, messages=("msg_0123456789abcdef",))
    assert need(mark, known) is R.AGENT_EVIDENCE
    assert need(mark, known, other=R.HUMAN_CONFIRMATION) is R.HUMAN_CONFIRMATION


def test_g12c_calendar_needs_the_human_only_to_invite_a_new_stranger() -> None:
    plain = CalendarCreate.model_validate(
        {
            "source": CALENDAR,
            "event": {"title": "t", "start": "2026-02-01T12:00:00Z", "end": "2026-02-01T13:00:00Z"},
        }
    )
    guests = plain.model_copy(
        update={"event": plain.event.model_copy(update={"attendees": ("a@example.org",)})}
    )
    delete = CalendarDelete(source=CALENDAR, event_ref="cev_0123456789abcdef", expected_etag="e")
    known = frozenset({"a@example.org"})
    assert need(plain, frozenset()) is R.AGENT_EVIDENCE
    assert need(guests, known) is R.AGENT_EVIDENCE
    assert need(guests, frozenset()) is R.HUMAN_CONFIRMATION
    assert need(delete, frozenset()) is R.AGENT_EVIDENCE
    assert need(delete, existing_attendees=True) is R.AGENT_EVIDENCE


def test_g12_config_may_tighten_but_never_loosen() -> None:
    assert check_config_requirement(R.HUMAN_CONFIRMATION) is R.HUMAN_CONFIRMATION
    with pytest.raises(ValueError, match="configured"):
        check_config_requirement(R.NONE)
    assert stricter(R.NONE, R.AGENT_EVIDENCE) is R.AGENT_EVIDENCE
    for bad in (
        {"send_known": R.NONE},
        {"other": R.NONE},
        {"zone": "Mars/Base"},
        {"sends_per_hour": 21},
        {"sends_per_hour": 0},
    ):
        with pytest.raises(ChamberlainError) as raised:
            WritePolicy(**bad)  # type: ignore[arg-type]
        assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_g1_a_standing_send_scope_names_recipients_and_a_daily_limit() -> None:
    kinds = frozenset({IntentKind.MAIL_SEND})
    with pytest.raises(ValidationError):
        StandingScope(source=MAIL, intent_kinds=kinds, title="t", max_per_day=3)
    with pytest.raises(ValidationError):
        StandingScope(source=MAIL, intent_kinds=kinds, title="t", recipients=frozenset({"a@b.co"}))
    with pytest.raises(ValidationError):
        StandingScope(source=MAIL, intent_kinds=frozenset(), title="t")


# --- W-22: exact-field scope matching -----------------------------------------------------


def scope(**kwargs: object) -> StandingScope:
    base: dict[str, object] = {
        "source": MAIL,
        "intent_kinds": frozenset({IntentKind.MAIL_SEND}),
        "title": "t",
        "recipients": frozenset({"friend@example.org"}),
        "max_per_day": 3,
    }
    return StandingScope.model_validate({**base, **kwargs})


def test_w22_matching_is_exact_never_fuzzy() -> None:
    assert not scope_mismatch(scope(), SEND, None)
    other = SEND.model_copy(update={"to": ("friend@example.org.evil.test",)})
    assert scope_mismatch(scope(), other, None)
    assert scope_mismatch(scope(recipients=frozenset({"friend@example.org"})), other, None)
    sub = SEND.model_copy(update={"to": ("a@mail.example.org",)})
    domain = scope(recipients=frozenset(), recipient_domains=frozenset({"example.org"}))
    assert scope_mismatch(domain, sub, None)
    same = SEND.model_copy(update={"to": ("a@example.org",)})
    assert not scope_mismatch(domain, same, None)
    assert scope_mismatch(scope(), SEND.model_copy(update={"cc": ("x@example.org",)}), None)
    assert scope_mismatch(scope(), SEND.model_copy(update={"source": "mail:other"}), None)


def test_w22_kind_card_folder_and_read_state_are_each_exact() -> None:
    assert scope_mismatch(scope(intent_kinds=frozenset({IntentKind.MAIL_DRAFT_SAVE})), SEND, None)
    assert scope_mismatch(scope(card="card_0123456789abcdef"), SEND, None)
    assert not scope_mismatch(scope(card="card_0123456789abcdef"), SEND, "card_0123456789abcdef")
    move = MailMove(source=MAIL, message="msg_0123456789abcdef", to_folder="f-archive")
    mover = scope(intent_kinds=frozenset({IntentKind.MAIL_MOVE}), recipients=frozenset())
    assert scope_mismatch(mover, move, None)
    filing = scope(
        intent_kinds=frozenset({IntentKind.MAIL_MOVE}),
        recipients=frozenset(),
        folders=frozenset({"f-archive"}),
    )
    assert not scope_mismatch(filing, move, None)
    reading = move.model_copy(update={"mark_read": True})
    assert scope_mismatch(filing, reading, None)
    allowed = filing.model_copy(update={"allow_mark_read": True})
    assert not scope_mismatch(allowed, reading, None)
    trash = MailTrash(source=MAIL, message="msg_0123456789abcdef", from_folder="f-inbox")
    trashing = scope(
        intent_kinds=frozenset({IntentKind.MAIL_TRASH}),
        recipients=frozenset(),
        folders=frozenset({"f-inbox"}),
    )
    assert not scope_mismatch(trashing, trash, None)
    assert scope_mismatch(trashing, trash.model_copy(update={"from_folder": "f-x"}), None)
    mark = MailMarkRead(source=MAIL, messages=("msg_0123456789abcdef",))
    marker = scope(intent_kinds=frozenset({IntentKind.MAIL_MARK_READ}), recipients=frozenset())
    assert scope_mismatch(marker, mark, None)
    assert not scope_mismatch(marker.model_copy(update={"allow_mark_read": True}), mark, None)


def test_w22_a_standing_scope_never_covers_a_calendar_update_it_cannot_see(rig: WriteRig) -> None:
    from chamberlain.domain.intents import CalendarDelete

    delete = CalendarDelete(source=CALENDAR, event_ref="cev_0123456789abcdef", expected_etag='"e1"')
    broad = scope(
        source=CALENDAR,
        intent_kinds=frozenset({IntentKind.CALENDAR_DELETE}),
        recipients=frozenset(),
    )
    assert scope_mismatch(broad, delete, None)
