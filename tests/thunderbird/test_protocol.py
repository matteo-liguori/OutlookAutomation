"""Protocol models, closed command list and generated types (bridge contract B-2, section 9)."""

import json
from pathlib import Path

import pytest

from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.protocol import (
    COMMAND_MODELS,
    Command,
    parse_args,
    parse_frame,
)
from chamberlain.connectors.thunderbird.typegen import render, write_generated
from chamberlain.schema.errors import ChamberlainError, ErrorCode

ID = "req_0123456789"


def test_b2_every_command_has_an_args_and_a_result_model() -> None:
    assert set(COMMAND_MODELS) == set(Command)


def test_b2_the_command_list_is_the_one_in_the_brief() -> None:
    reads = {
        "status",
        "folders",
        "query",
        "read",
        "attachment",
        "refresh",
        "contacts",
        "contact_read",
        "copies",
        "calendar_list",
        "calendar_query",
        "calendar_read",
        "quit",
    }
    writes = {"compose", "change", "draft_discard", "calendar_write"}
    assert {c.value for c in Command} == reads | writes
    assert {c.value for c in protocol.WRITE_COMMANDS} == writes


def test_b2_a_command_outside_the_list_is_refused() -> None:
    with pytest.raises(ChamberlainError) as raised:
        parse_args("eval", {})
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_b2_args_reject_unknown_and_malformed_fields() -> None:
    with pytest.raises(ChamberlainError) as raised:
        parse_args("folders", {"account_id": "a", "mailbox": "m@example.org", "shell": "x"})
    assert raised.value.code is ErrorCode.REQUEST_INVALID


@pytest.mark.parametrize(
    ("cmd", "required"),
    [
        ("change", {"items": [{"folder_id": "Inbox", "ids": ["a@x"]}], "trash": True}),
        ("copies", {"where": [{"folder_id": None, "ids": ["a@x"]}]}),
        ("calendar_write", {"kind": "delete", "calendar_id": "c", "event_id": "e"}),
    ],
)
def test_b2_args_require_the_fields_that_make_each_operation_safe(cmd, required) -> None:
    base = {} if cmd == "calendar_write" else {"account_id": "a", "mailbox": "m@example.org"}
    with pytest.raises(ChamberlainError) as raised:
        parse_args(cmd, {**base, **required})
    assert raised.value.code is ErrorCode.REQUEST_INVALID


def test_b2_a_response_carries_a_result_exactly_when_ok() -> None:
    ok = {"t": "response", "v": 3, "id": ID, "cmd": "status", "ok": True, "result": {}}
    assert parse_frame(json.dumps(ok).encode()).t == "response"
    bad = {"t": "response", "v": 3, "id": ID, "cmd": "status", "ok": True}
    with pytest.raises(ChamberlainError):
        parse_frame(json.dumps(bad).encode())


def test_b2_ids_are_required_on_every_message() -> None:
    with pytest.raises(ChamberlainError):
        parse_frame(b'{"t":"request","v":3,"cmd":"status","args":{}}')


def test_b2_the_generated_typescript_can_be_recreated(tmp_path: Path) -> None:
    generated = write_generated(tmp_path / "protocol.generated.ts")
    assert generated.read_text(encoding="utf-8") == render()
