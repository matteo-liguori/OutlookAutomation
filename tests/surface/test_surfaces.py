"""CLI and MCP stub: docs/contracts/02-tools.md (T-14 to T-16)."""

import io
import json
from pathlib import Path
from typing import Any

import pytest

from chamberlain.connections.load import profile_path
from chamberlain.schema.registry import STANDING_PROMPT, TOOLS
from chamberlain.surface import cli, mcp_server

NAMES = [spec.name.value for spec in TOOLS]


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), out)
    return code, out.getvalue()


@pytest.mark.parametrize("action", ["plan", "grant", "run"])
def test_s16_cleanup_reports_malformed_profile(data_root: Path, action: str) -> None:
    profile = profile_path(data_root)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("cleanup: [invalid", encoding="utf-8")

    code, text = run_cli("--data", str(data_root), "cleanup", action)

    assert code == cli.EXIT_FAILED
    assert text == (
        "The connection profile cannot be read. "
        "Correct config/connections.yaml and run `chamberlain config validate`.\n"
    )


def test_t15_cli_rejects_a_request_that_is_not_a_json_object_and_3_related_checks(
    data_root: Path,
) -> None:
    # cli_rejects_a_request_that_is_not_a_json_object
    code, text = run_cli("--data", str(data_root), "doctor", "--json", "[1, 2")
    assert code == cli.EXIT_INVALID
    assert json.loads(text)["error"]["code"] == "request.invalid"

    # t15_mcp_lists_one_tool_per_registry_entry_with_its_schema
    listed = mcp_server.list_tools()
    assert [tool["name"] for tool in listed] == NAMES
    for tool, spec in zip(listed, TOOLS, strict=True):
        assert tool["inputSchema"] == spec.input_schema()
        assert tool["description"] == spec.description

    # mcp_stub_answers_initialize_and_tools_list
    answers = _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
    )
    assert [answer["id"] for answer in answers] == [1, 2, 3]
    assert answers[0]["result"]["instructions"] == STANDING_PROMPT
    assert len(answers[1]["result"]["tools"]) == 12
    assert answers[2]["error"]["code"] == -32601

    # mcp_stub_survives_a_malformed_line
    stdin = io.StringIO('{"jsonrpc": \n{"jsonrpc": "2.0", "id": 9, "method": "ping"}\n')
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout)
    answers = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert answers[0]["error"]["code"] == -32700
    assert answers[1] == {"jsonrpc": "2.0", "id": 9, "result": {}}


def _rpc(*messages: dict[str, Any]) -> list[dict[str, Any]]:
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_status_lists_enabled_capabilities_and_disabled_sources(data_root: Path) -> None:
    profile = profile_path(data_root)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "sources:\n  calendar:work:\n    kind: google\n"
        "    account: test@example.org\n    calendar_id: primary\n"
        "  calendar:off:\n    kind: owl\n    calendar_id: other\n    enabled: false\n",
        encoding="utf-8",
    )
    code, text = run_cli("--data", str(data_root), "status")
    assert code == 0
    rows = json.loads(text)["sources"]
    assert {capability["name"] for capability in rows["calendar:work"]["capabilities"]} == {
        "calendar.read",
        "calendar.write",
        "calendar.invite",
        "calendar.recurrence",
        "calendar.occurrence",
    }
    assert rows["calendar:off"] == {"enabled": False, "kind": "owl", "capabilities": []}
