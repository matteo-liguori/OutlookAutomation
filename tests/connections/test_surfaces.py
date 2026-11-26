"""The CLI and MCP faces run on the configured app: docs/contracts/02-tools.md (T-14 to T-16)."""

import io
import json
from codecs import BOM_UTF8
from pathlib import Path
from typing import Any

import pytest
from conn_support import full_profile, write_profile

from chamberlain.connections.load import load_connections
from chamberlain.surface import cli, mcp_server


def run_cli(*argv: str) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = cli.main(list(argv), out)
    return code, json.loads(out.getvalue())


def test_s16_s17_config_validate_lists_the_sources_without_and_3_related_checks(
    data_root: Path,
) -> None:
    # s16_config_validate_lists_the_sources_without_touching_secrets
    write_profile(data_root, full_profile())
    code, shown = run_cli("--data", str(data_root), "config", "validate")
    assert code == cli.EXIT_OK
    assert shown["ok"] is True
    assert shown["sources"]["calendar:icloud"] == {"kind": "caldav", "enabled": True}
    assert "calendar:primary" in shown["connectors"]

    # s17_config_show_prints_the_profile_and_no_secret
    write_profile(data_root, full_profile())
    code, shown = run_cli("--data", str(data_root), "config", "show")
    assert code == cli.EXIT_OK
    assert shown["sources"]["mail:work"]["mailbox"] == "me@example.test"
    assert "password" not in json.dumps(shown).replace("password:me@icloud.example", "")

    # s16_config_validate_reports_a_bad_profile_as_an_error_reply
    write_profile(data_root, {"version": 1, "timezone": "Nowhere/None"})
    code, reply = run_cli("--data", str(data_root), "config", "validate")
    assert code == cli.EXIT_FAILED
    assert reply["error"]["code"] == "config.invalid"
    assert reply["error"]["field"] == "timezone"

    # s16_a_tool_call_reports_a_bad_profile_instead_of_crashing
    write_profile(data_root, {"version": 7})
    code, reply = run_cli("--data", str(data_root), "doctor")
    assert code == cli.EXIT_FAILED
    assert reply["error"]["code"] == "config.invalid"


def test_s16_the_tick_reports_a_bad_profile_instead_of_crashing(data_root: Path) -> None:
    write_profile(data_root, {"version": 7})
    code, reply = run_cli("--data", str(data_root), "tick")
    assert code == cli.EXIT_FAILED
    assert reply["error"]["code"] == "config.invalid"


def test_s16_config_commands_need_a_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cli.DATA_ENV, raising=False)
    code, reply = run_cli("config", "show")
    assert code == cli.EXIT_FAILED
    assert reply["error"]["code"] == "config.invalid"


@pytest.mark.parametrize(
    ("before_section", "after_section"),
    [
        ("", 'live_test:\n  calendars:\n    "calendar:work": {}\n'),
        (
            "live_test: {enabled: false, calendars: {}} # preserve this comment\n",
            'live_test: {enabled: false, calendars: {"calendar:work": {}}} '
            "# preserve this comment\n",
        ),
        (
            "live_test:\n  enabled: false\n  calendars:\n    calendar:primary: {} # keep\n",
            "live_test:\n  enabled: false\n  calendars:\n"
            '    "calendar:work": {}\n    calendar:primary: {} # keep\n',
        ),
    ],
)
def test_s16_add_test_calendar_preserves_yaml_and_backs_up_original(
    data_root: Path, before_section: str, after_section: str
) -> None:
    profile = write_profile(data_root, full_profile())
    sources = profile.read_text(encoding="utf-8")
    comment = "# Connection profile\n"
    original = BOM_UTF8 + (comment + before_section + sources).replace("\n", "\r\n").encode()
    profile.write_bytes(original)
    command = ("--data", str(data_root), "config", "add-test-calendar", "calendar:work")

    code, reply = run_cli(*command)

    assert code == cli.EXIT_OK
    assert reply["changed"] is True
    backup = Path(reply["backup"])
    assert backup.parent == profile.parent
    assert backup.read_bytes() == original
    expected = BOM_UTF8 + (comment + after_section + sources).replace("\n", "\r\n").encode()
    assert profile.read_bytes() == expected
    live = load_connections(data_root).live_test
    assert "calendar:work" in live.calendars
    assert live.enabled is False
    assert live.approve_calendar_edits is False

    assert run_cli(*command) == (
        cli.EXIT_OK,
        {"ok": True, "source": "calendar:work", "changed": False},
    )
    assert profile.read_bytes() == expected
    assert sorted(profile.parent.glob("connections.yaml.bak-*")) == [backup]

    code, next_reply = run_cli(*command[:-1], "calendar:icloud")
    assert code == cli.EXIT_OK
    next_backup = Path(next_reply["backup"])
    assert next_backup != backup
    assert next_backup.read_bytes() == expected
    assert backup.read_bytes() == original


@pytest.mark.parametrize("source", ["mail:work", "calendar:missing", "calendar:work"])
def test_s16_add_test_calendar_refuses_unknown_sources_or_invalid_profiles(
    data_root: Path, source: str
) -> None:
    document = full_profile()
    if source == "calendar:work":
        document["timezone"] = "Nowhere/None"
    profile = write_profile(data_root, document)
    original = profile.read_bytes()

    code, reply = run_cli("--data", str(data_root), "config", "add-test-calendar", source)

    assert code == cli.EXIT_FAILED
    assert reply["error"]["code"] == "config.invalid"
    assert profile.read_bytes() == original
    assert list(profile.parent.iterdir()) == [profile]


def test_t15_the_mcp_server_calls_tools_on_the_configured_app(data_root: Path) -> None:
    write_profile(data_root, full_profile() | {"sources": {}})
    caller = mcp_server.configured_caller(data_root)
    reply = caller("doctor", {})
    assert reply.ok


def test_t15_the_mcp_server_answers_a_bad_profile_with_an_error_reply(data_root: Path) -> None:
    write_profile(data_root, {"version": 7})
    reply = mcp_server.configured_caller(data_root)("doctor", {})
    assert not reply.ok
    assert reply.error is not None
    assert reply.error.code.value == "config.invalid"
