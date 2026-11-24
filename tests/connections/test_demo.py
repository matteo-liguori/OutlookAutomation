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


def test_s16_a_demo_source_validates_for_mail_calendar_and_contacts() -> None:
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


def test_s16_a_demo_source_rejects_unknown_keys() -> None:
    with pytest.raises(ChamberlainError):
        parse_connections(
            {"version": 1, "sources": {"mail:demo": {"kind": "demo", "password": "x"}}}
        )


def test_s16_the_router_builds_the_fake_provider_for_demo_sources() -> None:
    router = build_router(demo_profile(), Path("unused"), ManualClock())
    assert isinstance(router.mail_port("mail:demo"), FakeMail)
    assert isinstance(router.calendar_port("calendar:demo"), FakeCalendar)
    assert isinstance(router.contacts_port("contacts:demo"), FakeContacts)
    assert router.demo == {"mail:demo", "calendar:demo", "contacts:demo"}


def test_s16_the_demo_mailbox_is_obviously_synthetic() -> None:
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
