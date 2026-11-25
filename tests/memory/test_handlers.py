"""The five tools through App.call, with golden replies.

Contract: docs/contracts/02-tools.md (find, open, keep, forget, card) and
03-envelope-errors.md. Random handles are numbered in order of appearance before
a reply is compared with its golden file; set CHAMBERLAIN_UPDATE_GOLDEN=1 to
rewrite the files after an intended change.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from memory_helpers import SESSION, add_grant, add_operation, blobs, normalise

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.app.doctor import make_doctor
from chamberlain.app.doubles import RecordingWriteControl
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.domain.mail import MailQuery
from chamberlain.limits import MAX_REPLY_CHARS, PAGE_CHARS
from chamberlain.memory.handlers import DOCTOR_CHECKS, HANDLERS
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import Reply
from chamberlain.schema.tools import CardResponse, FindResponse, KeepResponse

GOLDEN = Path(__file__).parent / "golden"

pytestmark = pytest.mark.usefixtures("session")


def call(app: App, tool: str, **request: Any) -> Reply[Model]:
    reply = app.call(tool, {"session": SESSION, **request})
    assert len(reply.model_dump_json(exclude_none=True)) <= MAX_REPLY_CHARS  # E-2
    return reply


def golden(name: str, reply: Reply[Model]) -> None:
    text = normalise(json.dumps(reply.model_dump(mode="json", exclude_none=True), indent=2))
    path = GOLDEN / f"{name}.json"
    if os.environ.get("CHAMBERLAIN_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == text + "\n", f"{name} differs from its golden file"


def card_of(reply: Reply[Model]) -> Any:
    assert isinstance(reply.data, CardResponse), reply.error
    return reply.data.card


def test_t12_card_create_add_action_update_and_close_golden_and_1_related_checks(
    app: App, ctx: Context
) -> None:
    # t12_card_create_add_action_update_and_close_golden
    created = call(
        app, "card", op="create", fields={"title": "Supplier quote", "goal": "Agree price"}
    )
    golden("card_create", created)
    handle = card_of(created).handle
    added = call(
        app,
        "card",
        op="add_action",
        card=handle,
        action_fields={"title": "Send the quote", "due_at": "2026-01-09T09:00:00Z"},
    )
    golden("card_add_action", added)
    action = card_of(added).actions[0].handle
    updated = call(
        app,
        "card",
        op="update",
        card=handle,
        expected_version=2,
        fields={"position": "Quote drafted"},
    )
    assert card_of(updated).version == 3
    conflict = call(app, "card", op="update", card=handle, expected_version=2, fields={"goal": "x"})
    golden("card_version_conflict", conflict)
    finished = call(app, "card", op="finish_action", action=action, outcome="Quote sent.")
    assert card_of(finished).actions == ()
    closed = call(app, "card", op="close", card=handle, outcome="Agreed at the quoted price.")
    golden("card_close", closed)
    assert card_of(closed).state == "closed"

    # t12_over_cap_update_names_the_field_and_limit
    handle = card_of(call(app, "card", op="create", fields={"title": "Capped"})).handle
    too_long = app.call(
        "card",
        {"session": SESSION, "op": "update", "card": handle, "fields": {"goal": "g" * 401}},
    )
    assert too_long.error is not None  # rejected by the request schema before any write
    assert too_long.error.field is not None
    assert "goal" in too_long.error.field


def test_t10_keep_note_evidence_and_artifact_golden(app: App) -> None:
    card = card_of(call(app, "card", op="create", fields={"title": "Keeps things"})).handle
    action = (
        card_of(
            call(app, "card", op="add_action", card=card, action_fields={"title": "Do the thing"})
        )
        .actions[0]
        .handle
    )
    note = call(app, "keep", kind="note", owner=action, reason="scratch", text="Remember the VAT")
    golden("keep_note", note)
    evidence = call(
        app, "keep", kind="evidence", owner=card, reason="approval", text="User approved the quote."
    )
    golden("keep_evidence", evidence)
    artifact = call(
        app, "keep", kind="artifact", owner=action, reason="draft text", text="Dear supplier,"
    )
    golden("keep_artifact", artifact)
    assert isinstance(artifact.data, KeepResponse)
    assert artifact.data.sha256 == sha256_bytes(b"Dear supplier,")
    opened = call(app, "open", handle=artifact.data.handle, part="body")
    golden("open_artifact_body", opened)
    viewed = call(app, "open", handle=card)
    golden("open_card", viewed)
    found = call(app, "find", scope="cards", query="keeps")
    golden("find_cards", found)
    assert isinstance(found.data, FindResponse)
    forgotten = call(app, "forget", handle=artifact.data.handle)
    golden("forget_artifact", forgotten)


def test_t10_keep_without_a_live_owner_is_refused_with_a_fix_golden(app: App, ctx: Context) -> None:
    unknown = call(
        app, "keep", kind="artifact", owner="act_0123456789abcdef", reason="r", text="orphan bytes"
    )
    golden("keep_no_owner", unknown)
    assert blobs(ctx.data_root) == set()
    card = card_of(call(app, "card", op="create", fields={"title": "Evidence only"})).handle
    refused = call(app, "keep", kind="artifact", owner=card, reason="r", text="body")
    assert refused.error is not None
    assert refused.error.code.value == "artifact.no_owner"
    ended_action = (
        card_of(call(app, "card", op="add_action", card=card, action_fields={"title": "short"}))
        .actions[0]
        .handle
    )
    call(app, "card", op="finish_action", action=ended_action, outcome="Done.")
    ended = call(app, "keep", kind="note", owner=ended_action, reason="r", text="late")
    assert ended.error is not None
    assert ended.error.code.value == "artifact.owner_ended"


def test_t11_m20_forget_routes_grants_and_operations_to_writes_and_1_related_checks(
    app: App, ctx: Context, clock: ManualClock, fake: FakeProvider
) -> None:
    # t11_forget_routes_grants_and_operations_to_writes
    add_grant(ctx.db, clock, "grant_route00000001")
    add_operation(ctx.db, clock, "op_route000000001", "proposed")
    grant = call(app, "forget", handle="grant_route00000001")
    operation = call(app, "forget", handle="op_route000000001")
    assert grant.ok
    assert operation.ok
    writes = ctx.services.writes
    assert isinstance(writes, RecordingWriteControl)
    assert writes.revoked == ["grant_route00000001"]
    assert writes.cancelled == ["op_route000000001"]
    assert call(app, "forget", handle="card_0123456789abcdef").error is not None

    # m20_keep_caches_a_body_and_an_attachment_only_with_an_owner
    fake.mail.add_message(
        "mail:test",
        internet_message_id="<cache@x>",
        body="Cache this body",
        attachments=(("quote.pdf", b"%PDF-bytes"),),
    )
    found = call(app, "find", scope="mail")
    assert isinstance(found.data, FindResponse)
    message = found.data.hits[0].handle
    card = card_of(call(app, "card", op="create", fields={"title": "Caches"})).handle
    action = (
        card_of(call(app, "card", op="add_action", card=card, action_fields={"title": "Read it"}))
        .actions[0]
        .handle
    )
    body = call(app, "keep", kind="artifact", owner=action, reason="body", from_handle=message)
    attachment = call(
        app,
        "keep",
        kind="artifact",
        owner=action,
        reason="the quote",
        from_handle=message,
        attachment="a1",
    )
    assert isinstance(body.data, KeepResponse)
    assert isinstance(attachment.data, KeepResponse)
    assert body.data.sha256 == sha256_bytes(b"Cache this body")
    assert attachment.data.sha256 == sha256_bytes(b"%PDF-bytes")
    assert call(app, "open", handle=attachment.data.handle).ok
    assert fake.mail.is_unread("mail:test", next(iter(_locators(fake))))
    without = call(
        app, "keep", kind="artifact", owner="op_nope0000000000", reason="r", from_handle=message
    )
    assert without.error is not None


def _locators(fake: FakeProvider) -> list[str]:
    return [m.locator for m in fake.mail.search("mail:test", MailQuery()).items]


def test_m17_t3_find_pages_stay_under_the_budget_and_replies_and_1_related_checks(
    app: App, fake: FakeProvider
) -> None:
    # m17_find_pages_stay_under_the_budget_and_replies_under_the_cap
    for index in range(60):
        fake.mail.add_message(
            "mail:test",
            internet_message_id=f"<p{index}@x>",
            subject="s" * 190,
            body="b" * 5000,
        )
    cursor: str | None = None
    seen = 0
    for _ in range(20):
        request: dict[str, Any] = {"scope": "mail", "limit": 25}
        if cursor:
            request["cursor"] = cursor
        reply = call(app, "find", **request)
        assert isinstance(reply.data, FindResponse)
        assert sum(len(h.model_dump_json(exclude_none=True)) for h in reply.data.hits) <= PAGE_CHARS
        seen += len(reply.data.hits)
        assert reply.coverage is not None
        if reply.coverage.complete:
            break
        cursor = reply.coverage.cursor
    assert seen == 60

    # t3_a_session_is_required
    reply = app.call("find", {"scope": "cards"})
    assert reply.error is not None
    assert reply.error.code.value == "session.required"


def test_m23_doctor_storage_checks_report_through_the_app(ctx: Context) -> None:
    handlers = {**HANDLERS, ToolName.DOCTOR: make_doctor(DOCTOR_CHECKS)}
    reply = App(ctx, handlers).call("doctor", {"check": "storage"})
    assert reply.ok
    data = reply.data
    assert data is not None
    ids = {c["id"] for c in data.model_dump()["checks"]}
    assert {"storage.sweep", "storage.deletions", "storage.orphans", "storage.budget"} <= ids
