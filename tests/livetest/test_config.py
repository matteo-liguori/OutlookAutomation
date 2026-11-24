"""The `live_test` section: off by default, strict, and tied to real sources."""

from typing import Any

import pytest

from chamberlain.connections.load import parse_connections
from chamberlain.schema.errors import ChamberlainError, ErrorCode

MAIL_A: dict[str, Any] = {
    "kind": "thunderbird",
    "account_id": "acct-a",
    "mailbox": "me@example.test",
    "inbox_folder_id": "a://INBOX",
    "sent_folder_id": "a://Sent",
    "draft_folder_id": "a://Drafts",
    "trash_folder_id": "a://Trash",
}
MAIL_B: dict[str, Any] = {**MAIL_A, "account_id": "acct-b", "mailbox": "me@other.test"}


def profile(live: dict[str, Any] | None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": 1,
        "sources": {
            "mail:a": MAIL_A,
            "mail:b": MAIL_B,
            "calendar:main": {
                "kind": "google",
                "account": "me@example.test",
                "calendar_id": "primary",
            },
        },
    }
    if live is not None:
        document["live_test"] = live
    return document


GOOD: dict[str, Any] = {
    "enabled": True,
    "mail": {"mail:a": {"address": "me@example.test"}, "mail:b": {"address": "me@other.test"}},
    "pairs": [{"source": "mail:a", "to": "mail:b"}],
    "calendars": {"calendar:main": {"invitee_mail": "mail:b"}},
}


def test_off_by_default_and_a_complete_section_is_accepted() -> None:
    assert parse_connections(profile(None)).live_test.enabled is False
    live = parse_connections(profile(GOOD)).live_test
    assert live.owned_addresses() == {"me@example.test", "me@other.test"}
    assert live.approve_calendar_edits is False


@pytest.mark.parametrize(
    "change",
    [
        {"surprise": 1},
        {"mail": {"mail:a": {"address": "me@example.test", "password": "x"}}},
        {"pairs": [{"source": "mail:a", "to": "mail:b", "extra": 1}]},
    ],
)
def test_unknown_keys_are_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ChamberlainError) as raised:
        parse_connections(profile({**GOOD, **change}))
    assert raised.value.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize(
    "change",
    [
        {"mail": {"mail:zzz": {"address": "me@example.test"}}},
        {"mail": {"mail:a": {"address": "someone.else@example.test"}}},
        {
            "mail": {
                "mail:a": {"address": "me@example.test"},
                "mail:b": {"address": "me@example.test"},
            }
        },
        {"pairs": [{"source": "mail:a", "to": "mail:a"}]},
        {"pairs": [{"source": "mail:a", "to": "mail:nowhere"}]},
        {"calendars": {"calendar:nowhere": {}}},
        {"calendars": {"calendar:main": {"invitee_mail": "mail:nowhere"}}},
        {
            "calendars": {
                "calendar:main": {"invitee_calendar": "calendar:main", "invitee_mail": "mail:a"}
            }
        },
        {"mail": {}, "calendars": {}},
    ],
)
def test_a_section_naming_the_wrong_things_is_rejected(change: dict[str, Any]) -> None:
    # case does not matter in an address, but it must be the source's own mailbox
    parse_connections(
        profile(
            {
                **GOOD,
                "mail": {"mail:a": {"address": "ME@Example.test"}},
                "pairs": [],
                "calendars": {},
            }
        )
    )
    with pytest.raises(ChamberlainError) as raised:
        parse_connections(profile({**GOOD, **change}))
    assert raised.value.code is ErrorCode.CONFIG_INVALID
