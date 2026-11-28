"""A search the add-on cannot scan in one query is walked back in date windows."""

import random
import time
from datetime import UTC, datetime, timedelta

from chamberlain.connectors.thunderbird import protocol
from chamberlain.connectors.thunderbird.client import Reply
from chamberlain.connectors.thunderbird.mail import ThunderbirdMail
from chamberlain.connectors.thunderbird.mail_reads import (
    MailLimits,
    ThunderbirdBinding,
    milliseconds,
)
from chamberlain.domain.mail import MailQuery, MessageSummary
from chamberlain.ports.clock import ManualClock
from chamberlain.ports.page import Page

SOURCE = "mail:deep"
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
FOLDER = "inbox"


def _key(header: protocol.Header) -> str:
    return f"{header.folder_id}\u0000{header.internet_message_id}"


class FakeBridge:
    """Answers QUERY like the add-on: a date range (both ends inclusive), at most `cap`
    messages scanned in an arbitrary order, then words, order and paging."""

    def __init__(self, dates: list[datetime], cap: int = 2000) -> None:
        self.cap = cap
        self.calls: list[protocol.QueryArgs] = []
        self.headers = [self._header(n, d) for n, d in enumerate(dates)]
        random.Random(7).shuffle(self.headers)  # Thunderbird's order is not by date

    @staticmethod
    def _header(n: int, when: datetime) -> protocol.Header:
        return protocol.Header(
            internet_message_id=f"m{n:05d}@x",
            subject="Invoice" if n % 3 == 0 else "Chat",
            date_ms=milliseconds(when),
            author="a@example.org",
            read=True,
            folder_id=FOLDER,
        )

    def call(self, command: protocol.Command, args: object, *, payloads: object = None) -> Reply:
        if command is protocol.Command.REFRESH:
            assert isinstance(args, protocol.RefreshArgs)
            return Reply(
                protocol.RefreshResult(
                    folders=tuple(
                        protocol.FolderRefresh(folder_id=f, success=True) for f in args.folder_ids
                    ),
                    provider_callback_verified=True,
                )
            )
        assert isinstance(args, protocol.QueryArgs)
        self.calls.append(args)
        lo = args.since_ms if args.since_ms is not None else -(10**15)
        hi = args.until_ms if args.until_ms is not None else 10**15
        ranged = [h for h in self.headers if lo <= h.date_ms <= hi]
        room = min(args.max_scan, self.cap)
        scanned = ranged[:room]
        words = (args.words or "").lower().split()
        hits = [h for h in scanned if all(w in h.subject.lower() for w in words)]
        hits.sort(key=lambda h: (h.date_ms, _key(h)), reverse=True)
        if args.after is not None:
            after = (args.after.date_ms, args.after.key)
            hits = [h for h in hits if (h.date_ms, _key(h)) < after]
        shown = hits[: args.limit]
        more = len(hits) > args.limit
        return Reply(
            protocol.QueryResult(
                messages=tuple(shown),
                more=more,
                complete=len(ranged) <= room,
                scanned=len(scanned),
                next=protocol.QueryCursor(date_ms=shown[-1].date_ms, key=_key(shown[-1]))
                if more and shown
                else None,
            )
        )


class Ticker:
    """A monotonic clock that advances on every read, so the budget runs out."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def mail(bridge: FakeBridge, ticker: Ticker | None = None) -> ThunderbirdMail:
    binding = ThunderbirdBinding(
        account_id="acc",
        mailbox="me@example.org",
        inbox_folder_id=FOLDER,
        sent_folder_id="sent",
        draft_folder_id="drafts",
        trash_folder_id="trash",
    )
    return ThunderbirdMail(
        bridge,  # type: ignore[arg-type]
        {SOURCE: binding},
        ManualClock(NOW),
        limits=MailLimits(sample_interval_seconds=0),
        monotonic=ticker or time.monotonic,
    )


def spread(count: int) -> list[datetime]:
    """One message every 7 hours, going back from the day before NOW (10,000 is about 8 years)."""
    return [NOW - timedelta(days=1, hours=7 * n) for n in range(count)]


def ids(page: Page[MessageSummary]) -> list[str]:
    return [item.internet_message_id for item in page.items]


def newest_invoices(bridge: FakeBridge, count: int) -> list[str]:
    hits = sorted(
        (h for h in bridge.headers if h.subject == "Invoice"),
        key=lambda h: (h.date_ms, h.internet_message_id),
        reverse=True,
    )
    return [f"<{h.internet_message_id}>" for h in hits[:count]]


def test_a_complete_first_query_is_one_call() -> None:
    bridge = FakeBridge(spread(300))
    page = mail(bridge).search(SOURCE, MailQuery(text="invoice", limit=10))
    assert len(bridge.calls) == 1
    assert ids(page) == newest_invoices(bridge, 10)
    assert page.cursor is not None
    assert not page.gaps


def test_an_incomplete_query_walks_back_to_the_true_newest_matches() -> None:
    bridge = FakeBridge(spread(10_000))
    page = mail(bridge).search(SOURCE, MailQuery(text="invoice", limit=25))
    assert ids(page) == newest_invoices(bridge, 25)
    assert not page.gaps
    assert page.cursor is not None


def test_a_search_with_no_matches_walks_to_the_floor_and_is_complete() -> None:
    bridge = FakeBridge(spread(10_000))
    page = mail(bridge).search(SOURCE, MailQuery(text="nothing-matches", limit=25))
    assert page.items == ()
    assert page.complete
    assert page.cursor is None


def test_paging_never_repeats_or_skips() -> None:
    bridge = FakeBridge(spread(10_000))
    reader = mail(bridge)
    query = MailQuery(text="invoice", limit=200)
    seen: list[str] = []
    cursor = None
    for _ in range(40):
        page = reader.search(SOURCE, query, cursor)
        seen.extend(ids(page))
        cursor = page.cursor
        if cursor is None:
            break
    assert cursor is None
    assert seen == newest_invoices(bridge, 10**6)


def test_the_time_budget_stops_with_a_gap_and_a_cursor_that_continues() -> None:
    bridge = FakeBridge(spread(10_000))
    query = MailQuery(text="nothing-matches", limit=25)
    reader = mail(bridge, Ticker(step=100.0))
    page = reader.search(SOURCE, query)
    gap = next(g for g in page.gaps if "reached back to" in g.reason)
    assert gap.reason.startswith("The search reached back to 20")
    assert not gap.narrow
    assert len(gap.reason.split("back to ")[1].split(" ")[0]) == 10  # YYYY-MM-DD
    assert page.cursor is not None
    assert not page.complete
    stopped_at = min(c.since_ms for c in bridge.calls if c.since_ms is not None)
    before = len(bridge.calls)
    reader.search(SOURCE, query, page.cursor)
    assert max(c.until_ms or 0 for c in bridge.calls[before:]) <= stopped_at + 1


def test_matches_found_before_the_budget_ran_out_continue_in_order() -> None:
    bridge = FakeBridge(spread(10_000))
    query = MailQuery(text="invoice", limit=200)
    reader = mail(bridge, Ticker(step=40.0))
    seen: list[str] = []
    cursor = None
    for _ in range(200):
        page = reader.search(SOURCE, query, cursor)
        seen.extend(ids(page))
        cursor = page.cursor
        if cursor is None:
            break
    assert cursor is None
    assert seen == newest_invoices(bridge, 10**6)


def test_a_dense_day_stays_incomplete_and_the_walk_ends() -> None:
    dense = [NOW - timedelta(days=100, seconds=n) for n in range(3000)]
    bridge = FakeBridge(dense + spread(50), cap=2000)
    page = mail(bridge).search(SOURCE, MailQuery(text="nothing-matches", limit=5))
    assert any(g.narrow and "scan limit" in g.reason for g in page.gaps)
    assert not page.complete
    assert len(bridge.calls) < 2000


def test_since_and_until_bound_the_walk() -> None:
    bridge = FakeBridge(spread(10_000), cap=300)
    since = NOW - timedelta(days=900)
    until = NOW - timedelta(days=600)
    query = MailQuery(text="nothing-matches", since=since, until=until, limit=5)
    page = mail(bridge).search(SOURCE, query)
    assert page.complete
    walked = bridge.calls[1:]
    assert max(c.until_ms or 0 for c in walked) <= milliseconds(until)
    assert min(c.since_ms or 0 for c in walked) >= milliseconds(since)
