"""find and open through App.call: docs/contracts/06-memory.md (M-17 to M-20), 02-tools.md
(T-8, T-9) and 03-envelope-errors.md (E-2, E-3, E-7)."""

from datetime import timedelta
from typing import Any

import pytest
from memory_helpers import OTHER_SESSION, SESSION, add_session, keep_text

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.connectors.fake import FakeProvider, Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.limits import MAX_REPLY_CHARS, PAGE_CHARS
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.memory.cards import CardService
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.base import Model
from chamberlain.schema.envelope import Reply
from chamberlain.schema.tools import FindResponse, OpenResponse

pytestmark = pytest.mark.usefixtures("session")


def call(app: App, tool: str, session: str = SESSION, **request: Any) -> Reply[Model]:
    reply = app.call(tool, {"session": session, **request})
    assert len(reply.model_dump_json(exclude_none=True)) <= MAX_REPLY_CHARS
    return reply


def hits(reply: Reply[Model]) -> list[Any]:
    assert isinstance(reply.data, FindResponse), reply.error
    return list(reply.data.hits)


def seed_mail(fake: FakeProvider, count: int, subject: str = "Quarterly figures") -> None:
    for index in range(count):
        fake.mail.add_message(
            "mail:test",
            internet_message_id=f"<m{index}@example.org>",
            subject=f"{subject} {index} " + "x" * 120,
            body="SECRET-BODY-TEXT must never appear in a list.",
            date=fake.clock.now() - timedelta(minutes=index),
        )


def test_m17_pages_respect_the_character_budget_and_cover_everything(
    app: App, fake: FakeProvider
) -> None:
    seed_mail(fake, 40)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        request: dict[str, Any] = {"scope": "mail", "limit": 25}
        if cursor:
            request["cursor"] = cursor
        reply = call(app, "find", **request)
        page = hits(reply)
        assert len(page) < 25  # the 4,000 character budget bites first
        assert sum(len(h.model_dump_json(exclude_none=True)) for h in page) <= PAGE_CHARS
        seen.extend(h.handle for h in page)
        pages += 1
        assert reply.coverage is not None
        if reply.coverage.complete:
            break
        assert reply.coverage.cursor is not None
        cursor = reply.coverage.cursor
    assert pages > 1
    assert len(seen) == len(set(seen)) == 40


def test_t8_find_never_returns_a_body_and_hit_lines_are_single_lines(
    app: App, fake: FakeProvider
) -> None:
    fake.mail.add_message(
        "mail:test",
        internet_message_id="<one@example.org>",
        subject="Line one\nLine two\r\nLine three",
        body="SECRET-BODY-TEXT",
    )
    reply = call(app, "find", scope="mail")
    assert "SECRET-BODY-TEXT" not in reply.model_dump_json()
    [hit] = hits(reply)
    assert hit.content is not None
    assert "\n" not in hit.content.text
    assert len(hit.content.text) <= 200
    assert hit.content.origin == hit.handle
    assert hit.summary == "unread"  # harness text only; sender text is inside content


def test_m17_a_cursor_is_immutable_session_bound_query_bound_and_expires(
    app: App, fake: FakeProvider, ctx: Context, clock: ManualClock
) -> None:
    seed_mail(fake, 30)
    first = call(app, "find", scope="mail", limit=25)
    assert first.coverage is not None
    cursor = first.coverage.cursor
    assert cursor is not None
    total = first.coverage.total
    seed_mail(fake, 5, subject="Arrived later")  # the collection changes after the cursor exists
    second = call(app, "find", scope="mail", cursor=cursor)
    assert second.coverage is not None
    assert second.coverage.total == total == 30
    assert second.coverage.complete or second.coverage.cursor is not None
    add_session(ctx.db, clock, OTHER_SESSION)
    stolen = call(app, "find", OTHER_SESSION, scope="mail", cursor=cursor)
    assert stolen.error is not None
    assert stolen.error.code.value == "request.bad_cursor"
    changed = call(app, "find", scope="mail", query="different question", cursor=cursor)
    assert changed.error is not None
    assert changed.error.code.value == "request.bad_cursor"
    clock.advance(hours=2, seconds=1)
    ctx.db.execute("UPDATE sessions SET last_seen_at = ?", ("2999-01-01T00:00:00Z",))
    late = call(app, "find", scope="mail", cursor=cursor)
    assert late.error is not None
    assert late.error.code.value == "cursor.expired"


def test_t9_find_and_open_leave_unread_mail_unread(app: App, fake: FakeProvider) -> None:
    locator = fake.mail.add_message(
        "mail:test",
        internet_message_id="<unread@x>",
        body="Hello there",
        in_reply_to="<first@x>",
        references=("<root@x>", "<first@x>"),
    )
    [hit] = hits(call(app, "find", scope="mail"))
    for part in ("summary", "headers", "body", "attachments"):
        reply = call(app, "open", handle=hit.handle, part=part)
        assert reply.ok, reply.error
        if part == "headers":
            assert isinstance(reply.data, OpenResponse)
            assert reply.data.content is not None
            assert "In-Reply-To: <first@x>" in reply.data.content.text
            assert "References: <root@x> <first@x>" in reply.data.content.text
    assert fake.mail.is_unread("mail:test", locator)
    opened = call(app, "open", handle=hit.handle, part="body")
    assert isinstance(opened.data, OpenResponse)
    assert opened.data.content is not None
    assert opened.data.content.text == "Hello there"
    assert opened.data.content.kind.value == "mail.body"
    assert opened.data.range is not None
    assert opened.data.range.total == len("Hello there")


def test_e3_a_stale_source_is_incomplete_not_empty(app: App, fake: FakeProvider) -> None:
    fake.arm(Fault.STALE)
    reply = call(app, "find", scope="mail")
    assert hits(reply) == []
    assert reply.coverage is not None
    assert not reply.coverage.complete
    assert [g.source for g in reply.coverage.gaps] == ["mail:test"]
    clean = call(app, "find", scope="mail")
    assert clean.coverage is not None
    assert clean.coverage.complete
    assert clean.coverage.gaps == ()


def test_e3_a_source_that_cannot_answer_becomes_a_gap(app: App, fake: FakeProvider) -> None:
    seed_mail(fake, 2)
    fake.arm(Fault.SLOW)
    reply = call(app, "find", scope="mail")
    assert reply.ok
    assert hits(reply) == []
    assert reply.coverage is not None
    assert not reply.coverage.complete
    assert reply.coverage.gaps[0].source == "mail:test"


def test_m20_distinct_copies_keep_distinct_handles_but_show_as_one_hit(
    app: App, fake: FakeProvider
) -> None:
    fake.mail.add_message("mail:test", internet_message_id="<dup@x>", folder_id="f-inbox")
    fake.mail.add_message("mail:test", internet_message_id="<dup@x>", folder_id="f-archive")
    everywhere = hits(call(app, "find", scope="mail"))
    assert len(everywhere) == 1
    assert everywhere[0].content is not None
    assert "[" in everywhere[0].content.text
    inbox = hits(call(app, "find", scope="mail", folder="inbox"))
    archive = hits(call(app, "find", scope="mail", folder="archive"))
    assert len(inbox) == len(archive) == 1
    assert inbox[0].handle != archive[0].handle
    assert everywhere[0].handle == inbox[0].handle


def test_t8_within_returns_matching_lines_with_offsets_that_open_can_read(
    app: App, ctx: Context, action: str
) -> None:
    store = ArtifactService(ctx.db, ctx.clock, ctx.data_root)
    text = "first line\nthe needle is here\nthird line\nanother needle too"
    handle = keep_text(store, action, text)
    found = hits(call(app, "find", scope="stored", within=handle, query="needle"))
    assert [h.offset for h in found] == [text.index("the needle"), text.index("another")]
    assert all(h.kind == "match" for h in found)
    opened = call(app, "open", handle=handle, part="body", offset=found[0].offset, length=17)
    assert isinstance(opened.data, OpenResponse)
    assert opened.data.content is not None
    assert opened.data.content.text == "the needle is her"
    assert opened.data.content.truncated
    assert opened.data.range is not None
    assert opened.data.range.total == len(text)


def test_t8_within_a_message_and_a_card(app: App, fake: FakeProvider, cards: CardService) -> None:
    fake.mail.add_message(
        "mail:test", internet_message_id="<body@x>", body="alpha\nbeta budget line\ngamma"
    )
    [message] = hits(call(app, "find", scope="mail"))
    found = hits(call(app, "find", scope="mail", within=message.handle, query="budget"))
    assert [h.offset for h in found] == [6]
    card = cards.create(title="Budget review", position="Waiting for the budget sign-off")
    inside = hits(call(app, "find", scope="cards", within=card.handle, query="budget"))
    assert len(inside) == 2
    needs_query = call(app, "find", scope="cards", within=card.handle)
    assert needs_query.error is not None
    assert needs_query.error.field == "query"


def test_m18_open_returns_one_bounded_range(app: App, ctx: Context, action: str) -> None:
    store = ArtifactService(ctx.db, ctx.clock, ctx.data_root)
    handle = keep_text(store, action, "z" * 9000)
    page = call(app, "open", handle=handle, part="body")
    assert isinstance(page.data, OpenResponse)
    assert page.data.content is not None
    assert len(page.data.content.text) == PAGE_CHARS
    assert page.data.content.truncated
    last = call(app, "open", handle=handle, part="body", offset=8000, length=4000)
    assert isinstance(last.data, OpenResponse)
    assert last.data.content is not None
    assert len(last.data.content.text) == 1000
    assert not last.data.content.truncated
    too_big = app.call("open", {"session": SESSION, "handle": handle, "length": 4001})
    assert too_big.error is not None


def test_m19_default_open_is_the_minimal_view(app: App, fake: FakeProvider) -> None:
    fake.mail.add_message(
        "mail:test", internet_message_id="<min@x>", body="BODY", attachments=(("a.pdf", b"%PDF"),)
    )
    [hit] = hits(call(app, "find", scope="mail"))
    default = call(app, "open", handle=hit.handle)
    assert isinstance(default.data, OpenResponse)
    assert default.data.content is None
    assert default.data.view.kind == "message"
    attachments = call(app, "open", handle=hit.handle, part="attachments")
    assert isinstance(attachments.data, OpenResponse)
    [info] = attachments.data.attachments
    assert info.content.text == "a.pdf"
    assert info.content.kind.value == "attachment.name"


def test_m9_open_with_lease_returns_a_path_that_expires(
    app: App, ctx: Context, action: str
) -> None:
    store = ArtifactService(ctx.db, ctx.clock, ctx.data_root)
    handle = keep_text(store, action, "file bytes", filename="report.txt")
    reply = call(app, "open", handle=handle, part="file", lease=True)
    assert isinstance(reply.data, OpenResponse)
    assert reply.data.lease is not None
    assert reply.data.lease.path.endswith("report.txt")
    assert (reply.data.lease.expires_at - ctx.clock.now()) == timedelta(minutes=30)


def test_t8_find_scopes_cards_stored_calendar_contacts_grants_operations(
    app: App, fake: FakeProvider, cards: CardService, ctx: Context, action: str
) -> None:
    cards.create(title="Supplier negotiation")
    assert [h.kind for h in hits(call(app, "find", scope="cards", query="negotiation"))] == ["card"]
    store = ArtifactService(ctx.db, ctx.clock, ctx.data_root)
    keep_text(store, action, "stored")
    assert [h.kind for h in hits(call(app, "find", scope="stored"))] == ["artifact"]
    fake.calendar.add_event(
        "calendar:test",
        EventFields(
            title="Planning call",
            start=ctx.clock.now() + timedelta(days=1),
            end=ctx.clock.now() + timedelta(days=1, hours=1),
        ),
    )
    [event] = hits(call(app, "find", scope="calendar"))
    assert event.handle.startswith("cev_")
    opened = call(app, "open", handle=event.handle, part="body")
    assert isinstance(opened.data, OpenResponse)
    assert opened.data.content is not None
    assert opened.data.content.kind.value == "calendar.event"
    fake.contacts.add_contact("contacts:test", "Alice Example", "alice@example.org")
    [contact] = hits(call(app, "find", scope="contacts", query="alice"))
    assert contact.handle.startswith("con_")
    shown = call(app, "open", handle=contact.handle)
    assert isinstance(shown.data, OpenResponse)
    assert shown.data.content is not None
    assert shown.data.content.kind.value == "contact"
    assert shown.data.content.text == "Alice Example\nalice@example.org"
    assert call(app, "find", scope="contacts").error is not None
    assert hits(call(app, "find", scope="grants")) == []
    assert hits(call(app, "find", scope="operations")) == []


def test_e13_unknown_and_wrong_kind_handles_have_fixes(app: App) -> None:
    unknown = call(app, "open", handle="msg_0123456789abcdef")
    assert unknown.error is not None
    assert unknown.error.code.value == "request.unknown_handle"
    note = call(app, "open", handle="note_0123456789abcdef")
    assert note.error is not None
    assert note.error.code.value == "request.wrong_kind"
