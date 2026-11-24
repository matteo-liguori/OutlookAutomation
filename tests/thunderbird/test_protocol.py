"""Protocol models, closed command list and generated types (bridge contract B-2, section 9)."""

import json

import pytest

from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.protocol import (
    ARGS_MODELS,
    RESULT_MODELS,
    Command,
    dump_frame,
    parse_args,
    parse_frame,
)
from chamberlain.connectors.thunderbird.typegen import GENERATED, render
from chamberlain.schema.errors import ChamberlainError, ErrorCode

ID = "req_0123456789"


def test_b2_every_command_has_an_args_and_a_result_model() -> None:
    assert set(ARGS_MODELS) == set(Command) == set(RESULT_MODELS)


def test_b2_the_command_list_is_the_one_in_the_brief() -> None:
    reads = {"status", "accounts", "folders", "query", "read", "attachment", "resolve"}
    reads |= {"refresh", "contacts", "contact_read"}
    writes = {"send", "draft_save", "draft_update", "draft_discard", "move", "trash", "mark_read"}
    twins = {f"reconcile_{name}" for name in writes - {"send"}} | {"reconcile_send"}
    calendar = {
        f"calendar_{name}" for name in ("list", "query", "read", "create", "update", "delete")
    }
    assert {c.value for c in Command} == reads | writes | twins | calendar
    assert {c.value for c in protocol.WRITE_COMMANDS} == writes | {
        "calendar_create",
        "calendar_update",
        "calendar_delete",
    }


def test_b2_a_command_outside_the_list_is_refused() -> None:
    with pytest.raises(ChamberlainError) as raised:
        parse_args("eval", {})
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_b2_args_reject_unknown_and_malformed_fields() -> None:
    with pytest.raises(ChamberlainError) as raised:
        parse_args("move", {"account_id": "a", "mailbox": "m@example.org", "shell": "x"})
    assert raised.value.code is ErrorCode.REQUEST_INVALID


def test_b2_a_response_carries_a_result_exactly_when_ok() -> None:
    ok = {"t": "response", "v": 2, "id": ID, "cmd": "status", "ok": True, "result": {}}
    assert parse_frame(json.dumps(ok).encode()).t == "response"
    bad = {"t": "response", "v": 2, "id": ID, "cmd": "status", "ok": True}
    with pytest.raises(ChamberlainError):
        parse_frame(json.dumps(bad).encode())


def test_b2_ids_are_required_on_every_message() -> None:
    with pytest.raises(ChamberlainError):
        parse_frame(b'{"t":"request","v":2,"cmd":"status","args":{}}')


def test_b2_frames_dump_compactly_and_parse_back() -> None:
    frame = protocol.RequestFrame(v=2, id=ID, cmd="status", args={})
    assert parse_frame(dump_frame(frame)) == frame


def test_b2_the_generated_typescript_is_up_to_date() -> None:
    assert GENERATED.read_text(encoding="utf-8") == render(), (
        "run: python -m chamberlain.connectors.thunderbird.typegen"
    )
