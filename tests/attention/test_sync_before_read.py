"""Mail reads on wired tool paths observe fresh folders or disclose a sync failure."""

from collections.abc import Collection, Sequence
from datetime import timedelta

import pytest

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.app.wiring import app_handlers
from chamberlain.attention import repo
from chamberlain.attention.handlers import HOOKS
from chamberlain.attention.tuning import SYNC_FRESH, SYNC_LIMIT
from chamberlain.connections.model import MailSyncConfig
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.events import EventKind
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.mail import MailQuery, Message, MessageSummary
from chamberlain.ports.clock import ManualClock
from chamberlain.ports.page import Page
from chamberlain.ports.sync import SyncReport
from chamberlain.schema.tools import FindResponse, NextResponse, OpenResponse, StartResponse

SOURCE = "mail:test"


@pytest.fixture
def observed_mail(fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    search = fake.mail.search
    read = fake.mail.read
    sync = fake.mail.sync_folders

    def before_search(
        source: str, query: MailQuery, cursor: str | None = None
    ) -> Page[MessageSummary]:
        events.append("search")
        return search(source, query, cursor)

    def before_read(source: str, locator: str) -> Message:
        events.append("read")
        return read(source, locator)

    def after_sync(
        source: str, folders: Sequence[str], time_limit: timedelta, retry: Collection[str]
    ) -> SyncReport:
        result = sync(source, folders, time_limit, retry)
        events.append("sync finished")
        return result

    monkeypatch.setattr(fake.mail, "search", before_search)
    monkeypatch.setattr(fake.mail, "read", before_read)
    monkeypatch.setattr(fake.mail, "sync_folders", after_sync)
    return events


def _start(app: App) -> str:
    reply = app.call("start", {})
    assert reply.ok
    assert isinstance(reply.data, StartResponse)
    return reply.data.session


@pytest.mark.usefixtures("seed")
@pytest.mark.parametrize("tool", ["start", "find", "open"])
def test_a24_a_stale_mail_source_is_synced_before_normal_tool_reads(
    tool: str, ctx: Context, clock: ManualClock, fake: FakeProvider, observed_mail: list[str]
) -> None:
    app = App(ctx, app_handlers(), HOOKS)
    defaults = MailSyncConfig()
    assert SYNC_FRESH == timedelta(minutes=defaults.fresh_minutes) == timedelta(minutes=10)
    assert SYNC_LIMIT == timedelta(seconds=defaults.time_limit_seconds) == timedelta(minutes=5)
    session = _start(app)
    observed_mail.clear()

    clock.advance(minutes=11)
    mid = "<after-sync@example.org>"
    locator = fake.mail.add_message(
        SOURCE, internet_message_id=mid, subject="After sync", unsynced=True
    )
    handle = ctx.services.refs.remember(HandleKind.MESSAGE, SOURCE, locator, mid)
    args = (
        {}
        if tool == "start"
        else {"session": session, "scope": "mail", "query": "After sync"}
        if tool == "find"
        else {"session": session, "handle": handle, "part": "body"}
    )
    reply = app.call(tool, args)
    assert reply.ok, reply.error
    assert observed_mail[0] == "sync finished"
    assert observed_mail.count("sync finished") == 1
    assert "search" in observed_mail
    if tool == "start":
        assert isinstance(reply.data, StartResponse)
        assert any(work.kind is EventKind.MAIL_NEW_UNMATCHED for work in reply.data.work)
        session = reply.data.session
    elif tool == "find":
        assert isinstance(reply.data, FindResponse)
        assert [hit.handle for hit in reply.data.hits] == [handle]
    else:
        assert isinstance(reply.data, OpenResponse)
        assert "read" in observed_mail
        assert reply.data.handle == handle
    assert fake.mail.is_unread(SOURCE, locator)

    observed_mail.clear()
    clock.advance(minutes=9)
    assert app.call("find", {"session": session, "scope": "mail", "query": "After sync"}).ok
    assert "search" in observed_mail
    assert "sync finished" not in observed_mail


@pytest.mark.usefixtures("seed")
def test_a25_a_stuck_folder_is_reported_while_other_folders_are_read(
    ctx: Context, clock: ManualClock, fake: FakeProvider, observed_mail: list[str]
) -> None:
    app = App(ctx, app_handlers(), HOOKS)
    session = _start(app)
    observed_mail.clear()
    clock.advance(minutes=11)
    fake.mail.stick_folder(SOURCE, "f-archive")
    fake.mail.add_message(
        SOURCE,
        folder_id="f-archive",
        internet_message_id="<hidden@example.org>",
        subject="Current hidden mail",
        unsynced=True,
    )
    visible = fake.mail.add_message(
        SOURCE,
        internet_message_id="<visible@example.org>",
        subject="Current visible mail",
        unsynced=True,
    )
    reply = app.call("find", {"session": session, "scope": "mail", "query": "Current"})
    assert reply.ok, reply.error
    assert observed_mail[0] == "sync finished"
    assert "search" in observed_mail
    assert isinstance(reply.data, FindResponse)
    [hit] = reply.data.hits
    assert ctx.services.refs.locate(hit.handle) == (SOURCE, visible)
    stale = [event for event in repo.live_events(ctx.db) if event.kind is EventKind.SOURCE_STALE]
    assert any(event.attrs.source == SOURCE and event.attrs.reason == "sync" for event in stale)
    assert fake.mail.is_unread(SOURCE, visible)
    next_reply = app.call("next", {"session": session})
    assert next_reply.ok
    assert isinstance(next_reply.data, NextResponse)
    assert any(
        work.kind is EventKind.SOURCE_STALE and "1 archive" in work.summary
        for work in next_reply.data.work
    )
