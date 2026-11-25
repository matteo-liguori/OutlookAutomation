"""The demo source: synthetic data on the fake provider that never leaves the process."""

import socket
from pathlib import Path
from typing import Any

import pytest
from conn_support import write_profile

from chamberlain.connections import build_router, load_connections, open_app, parse_connections
from chamberlain.connections.demo import DEMO_DOMAIN, DEMO_MARK, demo_profile
from chamberlain.connectors.fake import FakeCalendar, FakeContacts, FakeMail
from chamberlain.domain.mail import MailQuery
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError


def _profile() -> dict[str, Any]:
    return {"version": 1, "sources": {"mail:demo": {"kind": "demo"}}}


def test_s16_a_demo_source_validates_for_mail_calendar_and_and_3_related_checks() -> None:
    # s16_a_demo_source_validates_for_mail_calendar_and_contacts
    parsed = parse_connections(
        {
            "version": 1,
            "sources": {
                "mail:a": {"kind": "demo"},
                "calendar:a": {"kind": "demo"},
                "contacts:a": {"kind": "demo"},
            },
        }
    )
    assert sorted(parsed.sources) == ["calendar:a", "contacts:a", "mail:a"]

    # s16_a_demo_source_rejects_unknown_keys
    with pytest.raises(ChamberlainError):
        parse_connections(
            {"version": 1, "sources": {"mail:demo": {"kind": "demo", "password": "x"}}}
        )

    # s16_the_router_builds_the_fake_provider_for_demo_sources
    router = build_router(demo_profile(), Path("unused"), ManualClock())
    assert isinstance(router.mail_port("mail:demo"), FakeMail)
    assert isinstance(router.calendar_port("calendar:demo"), FakeCalendar)
    assert isinstance(router.contacts_port("contacts:demo"), FakeContacts)
    assert router.demo == {"mail:demo", "calendar:demo", "contacts:demo"}

    # s16_the_demo_mailbox_is_obviously_synthetic
    router = build_router(demo_profile(), Path("unused"), ManualClock())
    page = router.mail_port("mail:demo").search("mail:demo", MailQuery())
    assert page.items
    for item in page.items:
        assert DEMO_MARK in item.subject
        assert item.sender.address.endswith("@" + DEMO_DOMAIN)


def test_s16_a_demo_profile_works_with_the_network_closed(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a demo source tried to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    write_profile(data_root, _profile())
    app = open_app(data_root)
    try:
        reply = app.call("guide", {})
        assert reply.ok
        assert reply.coverage is not None
        assert reply.coverage.demo is True
    finally:
        app.close()
    assert load_connections(data_root).sources["mail:demo"].kind == "demo"


def test_s16_doctor_is_calm_about_a_demo_only_profile(data_root: Path) -> None:
    assert _doctor(data_root, _profile())["sources.demo"] == "ok"


def test_s16_doctor_warns_when_demo_and_real_sources_are_mixed(data_root: Path) -> None:
    mixed = {
        "version": 1,
        "sources": {
            "mail:demo": {"kind": "demo"},
            "calendar:owl": {"kind": "owl", "calendar_id": "cal-1"},
        },
    }
    checks = _doctor(data_root, mixed)
    assert checks["sources.demo_mixed"] == "warn"


def _doctor(data_root: Path, profile: dict[str, Any]) -> dict[str, str]:
    write_profile(data_root, profile)
    app = open_app(data_root)
    try:
        reply = app.call("doctor", {"check": "sources"})
    finally:
        app.close()
    assert reply.ok
    assert reply.data is not None
    return {c.id: c.status for c in reply.data.checks}  # type: ignore[attr-defined]
