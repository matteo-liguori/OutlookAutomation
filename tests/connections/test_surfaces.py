"""The CLI and MCP faces run on the configured app: docs/contracts/02-tools.md (T-14 to T-16)."""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from conn_support import full_profile, write_profile

from chamberlain.surface import cli, mcp_server


def run_cli(*argv: str) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = cli.main(list(argv), out)
    return code, json.loads(out.getvalue())


def test_s16_config_validate_lists_the_sources_without_touching_secrets(data_root: Path) -> None:
    write_profile(data_root, full_profile())
    code, shown = run_cli("--data", str(data_root), "config", "validate")
    assert code == cli.EXIT_OK
    assert shown["ok"] is True
    assert shown["sources"]["calendar:icloud"] == {"kind": "caldav", "enabled": True}
    assert "calendar:primary" in shown["connectors"]


def test_s17_config_show_prints_the_profile_and_no_secret(data_root: Path) -> None:
    write_profile(data_root, full_profile())
    code, shown = run_cli("--data", str(data_root), "config", "show")
    assert code == cli.EXIT_OK
    assert shown["sources"]["mail:work"]["mailbox"] == "me@example.test"
    assert "password" not in json.dumps(shown).replace("password:me@icloud.example", "")


def test_s16_config_validate_reports_a_bad_profile_as_an_error_reply(data_root: Path) -> None:
    write_profile(data_root, {"version": 1, "timezone": "Nowhere/None"})
    code, reply = run_cli("--data", str(data_root), "config", "validate")
    assert code == cli.EXIT_FAILED
    assert reply["error"]["code"] == "config.invalid"
    assert reply["error"]["field"] == "timezone"


def test_s16_a_tool_call_reports_a_bad_profile_instead_of_crashing(data_root: Path) -> None:
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
