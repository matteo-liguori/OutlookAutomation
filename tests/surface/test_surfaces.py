"""CLI and MCP stub: docs/contracts/02-tools.md (T-14 to T-16)."""

import io
import json
from pathlib import Path
from typing import Any

import pytest

from chamberlain.schema.registry import STANDING_PROMPT, TOOLS
from chamberlain.surface import cli, mcp_server

NAMES = [spec.name.value for spec in TOOLS]


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), out)
    return code, out.getvalue()


def test_t14_cli_lists_the_twelve_tools_and_3_related_checks(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cli_lists_the_twelve_tools
    code, text = run_cli("tools")
    assert code == 0
    assert [line.split()[0] for line in text.splitlines()] == NAMES
    code, text = run_cli("tools", "--json")
    assert [tool["name"] for tool in json.loads(text)] == NAMES

    # cli_prints_a_tool_schema_and_the_standing_prompt
    code, text = run_cli("schema", "propose")
    assert code == 0
    assert "intent" in json.loads(text)["request"]["properties"]
    assert run_cli("prompt") == (0, STANDING_PROMPT + "\n")

    # t14_cli_prints_the_envelope_and_maps_exit_codes
    code, text = run_cli("--data", str(data_root), "doctor", "--json", '{"check": "schema"}')
    assert code == cli.EXIT_OK
    assert json.loads(text)["ok"] is True
    code, text = run_cli("--data", str(data_root), "open", "--json", '{"handle": "nope"}')
    assert code == cli.EXIT_INVALID
    assert json.loads(text)["error"]["field"] == "handle"
    code, text = run_cli(
        "--data", str(data_root), "do", "--session", "ses_0123456789abcdef",
        "--json", '{"operation": "op_0123456789abcdef"}',
    )  # fmt: skip
    assert code == cli.EXIT_INVALID
    assert json.loads(text)["error"]["code"] == "request.unknown_handle"

    # cli_refuses_to_guess_a_data_root
    monkeypatch.delenv(cli.DATA_ENV, raising=False)
    code, text = run_cli("doctor")
    assert code == cli.EXIT_FAILED
    assert json.loads(text)["error"]["code"] == "config.invalid"


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
