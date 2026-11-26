# pyright: reportAttributeAccessIssue=false
"""The new-mail watermark over every folder: nothing is skipped and nothing is shown twice.

Contract: docs/contracts/07-attention.md (A-6, A-7, A-20 to A-23). Random histories over the fake
provider with a small page size so every run pages, narrows, resumes and recovers. Mail arrives in
the inbox, is filed straight into other folders, moves between folders, has Gmail-style label
copies, is sent by the owner, or lands in Junk and Trash (never to be seen). The owner says yes
whenever the scan asks about older mail.
"""

import dataclasses
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from chamberlain.app.context import Context
from chamberlain.attention import older, repo, tick, watermark
from chamberlain.attention.suggest import Suggester
from chamberlain.connectors.fake import FakeProvider
from chamberlain.connectors.fake.mail import FakeMail
from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.mail import FolderRole, MailQuery, MessageSummary, message_key
from chamberlain.ports.clock import ManualClock
from chamberlain.ports.page import Gap, Page

SOURCE = "mail:test"
OWNER = "owner@example.org"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class Record:
    kind: str
    """in, sent or junk."""
    arrived: datetime
    date: datetime


@pytest.fixture(autouse=True)
def _small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watermark, "MAIL_PAGE_LIMIT", 7)
    monkeypatch.setattr(watermark, "MAIL_MAX_PAGES", 2)


@pytest.fixture
def seed_sources(ctx: Context) -> None:
    for source in ("mail:test", "calendar:test", "contacts:test"):
        now = ctx.clock.now().strftime(STAMP)
        ctx.db.execute(
            "INSERT OR IGNORE INTO sources (id, kind, connector, created_at, updated_at) "
            "VALUES (?, ?, 'fake', ?, ?)",
            (source, source.split(":")[0], now, now),
        )


@pytest.fixture
def rows_added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Every row the scan ever stored, as (message key, sent), in order."""
    log: list[tuple[str, bool]] = []
    real = repo.add_seen

    def add_seen(db: Any, source: str, key: str, *args: Any, sent: bool) -> None:
        log.append((key, sent))
        real(db, source, key, *args, sent=sent)

    monkeypatch.setattr(repo, "add_seen", add_seen)
    return log


def at(text: str) -> datetime:
    return datetime.strptime(text, STAMP).replace(tzinfo=UTC)


def instrument(fake: FakeProvider) -> dict[str, Any]:
    """A search that can fail on its second page or stop at a scan limit, like Thunderbird."""
    real = fake.mail.search
    knobs: dict[str, Any] = {"cap": None, "page2": False}

    def search(source: str, query: MailQuery, cursor: str | None = None) -> Page[MessageSummary]:
        if cursor is not None and knobs["page2"]:
            knobs["page2"] = False
            raise RuntimeError("connection dropped")
        if knobs["cap"] is not None:
            matches = [m for m in fake.world.mail[source] if FakeMail._matches(m, query)]
            if len(matches) > knobs["cap"]:
                matches.sort(key=lambda m: (m.date, m.uid), reverse=True)
                return Page[MessageSummary](
                    items=tuple(fake.mail._summary(source, m) for m in matches[: query.limit]),
                    complete=False,
                    gaps=(Gap(source=source, reason="Stopped at its scan limit.", narrow=True),),
                )
        return real(source, query, cursor)

    fake.mail.search = search  # type: ignore[method-assign]
    return knobs


def run_tick(ctx: Context, clock: ManualClock) -> None:
    clock.advance(seconds=61)
    tick.tick(ctx, force=True)


def test_a20_a_failed_scan_commit_keeps_the_frontier_and_leaves_no_refs(
    ctx: Context, fake: FakeProvider, seed_sources: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert watermark.scan(ctx, SOURCE, HealthState.OK, Suggester(ctx))
    before = repo.watermark(ctx.db, SOURCE)
    fake.mail.add_message(SOURCE, internet_message_id="<scan-rollback@x>")

    def fail_save(*args: object, **kwargs: object) -> None:
        raise OSError("store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "save_scan", fail_save)
        with pytest.raises(OSError, match="store unavailable"):
            watermark.scan(ctx, SOURCE, HealthState.OK, Suggester(ctx))
    assert repo.watermark(ctx.db, SOURCE) == before
    assert keys_seen(ctx) == {}
    assert ctx.db.execute("SELECT count(*) FROM refs").fetchone()[0] == 0

    assert watermark.scan(ctx, SOURCE, HealthState.OK, Suggester(ctx))
    assert keys_seen(ctx) == {"scan-rollback@x": False}


def owner_says_yes(ctx: Context) -> None:
    """The owner answers every open question about older mail with yes."""
    rows = ctx.db.execute(
        "SELECT handle FROM events WHERE kind = 'source.stale' AND dedupe_key LIKE '%:older:%' "
        "AND state IN ('new', 'delivered')"
    ).fetchall()
    handles = [r["handle"] for r in rows]
    if handles:
        older.next_with_answers(ctx, handles, handles, actor="agent:test")


def keys_seen(ctx: Context) -> dict[str, bool]:
    rows = ctx.db.execute("SELECT message_key, sent FROM mail_seen WHERE source_id = ?", (SOURCE,))
    return {r["message_key"]: bool(r["sent"]) for r in rows}


def keys_seen_in(ctx: Context) -> set[str]:
    return {k for k, sent in keys_seen(ctx).items() if not sent}


def mark(ctx: Context) -> Any:
    return ctx.db.execute("SELECT * FROM mail_watermarks WHERE source_id = ?", (SOURCE,)).fetchone()


def kinds(ctx: Context) -> list[str]:
    rows = ctx.db.execute(
        "SELECT kind FROM events WHERE state IN ('new', 'delivered') ORDER BY kind"
    )
    return [r["kind"] for r in rows]


class Mailbox:
    """The fake mailbox plus a ledger of what was put there, so a test can say what must be seen."""

    def __init__(self, fake: FakeProvider, clock: ManualClock, rng: random.Random) -> None:
        self.fake, self.clock, self.rng = fake, clock, rng
        self.ledger: dict[str, Record] = {}
        self.counter = 0
        fake.mail.add_folder(SOURCE, "f-all", "All Mail", FolderRole.ARCHIVE)
        fake.mail.add_folder(SOURCE, "f-junk", "Spam", FolderRole.JUNK)
        fake.mail.add_folder(SOURCE, "f-receipts", "Receipts")

    def _put(self, kind: str, folder: str, prefix: str) -> None:
        self.counter += 1
        message_id = f"<{prefix}{self.counter}@x>"
        late = timedelta(minutes=self.rng.choice([0, 0, 0, 1, 5]))
        date = (
            self.clock.now()
            - late
            - timedelta(seconds=self.counter // 3, milliseconds=self.counter % 3 * 7)
        )
        self.fake.mail.add_message(
            SOURCE,
            folder_id=folder,
            internet_message_id=message_id,
            sender=OWNER if kind == "sent" else "alice@example.org",
            to=("bob@example.org",) if kind == "sent" else (OWNER,),
            date=date,
            unread=self.rng.random() < 0.7,
        )
        self.ledger[message_key(message_id, "")] = Record(kind, self.clock.now(), date)

    def add(self, how: str) -> None:
        if how == "inbox":
            self._put("in", "f-inbox", "m")
        elif how == "filed":
            self._put("in", self.rng.choice(["f-receipts", "f-archive", "f-all"]), "m")
        elif how == "sent":
            self._put("sent", "f-sent", "s")
        else:
            self._put("junk", self.rng.choice(["f-junk", "f-trash", "f-drafts"]), "j")

    def move(self) -> None:
        inbox = [m for m in self.fake.world.mail[SOURCE] if m.folder_id == "f-inbox"]
        if inbox:
            self.rng.choice(inbox).folder_id = self.rng.choice(["f-archive", "f-receipts", "f-all"])

    def label_copy(self) -> None:
        real = [m for m in self.fake.world.mail[SOURCE] if m.folder_id in ("f-inbox", "f-sent")]
        if not real:
            return
        original = self.rng.choice(real)
        self.fake.mail.add_message(
            SOURCE,
            folder_id="f-all",
            internet_message_id=original.internet_message_id,
            sender=original.sender.address,
            to=tuple(a.address for a in original.to),
            date=original.date,
        )

    def promised(self, ctx: Context) -> set[str]:
        """Keys the watermark has promised: all that arrived before it, bar what is held back."""
        state = mark(ctx)
        if state is None or state["scan_started_at"] is not None or state["gap_from"] is not None:
            return set()
        floor = at(state["watching_since"]) - timedelta(hours=48)
        if state["held_to"] is not None:
            floor = max(floor, at(state["held_to"]))
        through = at(state["covered_through"])
        return {
            key
            for key, rec in self.ledger.items()
            if rec.kind != "junk" and rec.arrived < through and rec.date >= floor
        }

    def wanted(self, ctx: Context) -> set[str]:
        """Everything the scan is allowed to have read once the owner has said yes."""
        floor = at(mark(ctx)["watching_since"]) - timedelta(hours=48)
        return {k for k, r in self.ledger.items() if r.kind != "junk" and r.date >= floor}


@pytest.mark.parametrize("seed", range(60))
def test_a_nothing_is_skipped_and_nothing_is_shown_twice_across_folders(
    seed: int,
    ctx: Context,
    fake: FakeProvider,
    clock: ManualClock,
    seed_sources: None,
    rows_added: list[tuple[str, bool]],
) -> None:
    rng = random.Random(seed)
    knobs = instrument(fake)
    box = Mailbox(fake, clock, rng)
    steps = ["add", "add", "burst", "filed", "sent", "junk", "move", "label", "tick", "tick"]
    steps += ["read", "wait", "stale", "raise", "restart", "page2", "cap", "yes"]
    for _ in range(rng.randint(25, 60)):
        step = rng.choice(steps)
        if step == "add":
            box.add("inbox")
        elif step == "burst":
            for _ in range(rng.randint(8, 40)):
                box.add(rng.choice(["inbox", "inbox", "filed", "sent", "junk"]))
        elif step in ("filed", "sent", "junk"):
            box.add(step)
        elif step == "move":
            box.move()
        elif step == "label":
            box.label_copy()
        elif step == "tick":
            run_tick(ctx, clock)
        elif step == "read":
            for message in fake.world.mail[SOURCE]:
                message.unread = False
        elif step == "wait":
            clock.advance(hours=rng.choice([1, 8, 30, 60]))
            if rng.random() < 0.5:
                fake.world.health_override.pop(SOURCE, None)
        elif step == "restart":
            fake.restart()
            ctx = dataclasses.replace(ctx)
        elif step == "page2":
            knobs["page2"] = True
        elif step == "cap":
            knobs["cap"] = rng.choice([None, 10, 14])
        elif step == "yes":
            owner_says_yes(ctx)
        elif step == "stale":
            fake.arm(Fault.STALE, times=rng.randint(1, 3))
        else:
            fake.world.health_override[SOURCE] = HealthState.UNAVAILABLE

        seen = {key for key, _ in rows_added}
        assert len(seen) == len(rows_added), (seed, "a message was stored twice")
        for key, sent in rows_added:
            assert key in box.ledger, (seed, key, "a message that does not exist was stored")
            assert box.ledger[key].kind != "junk", (seed, key, "junk or trash was read")
            assert sent == (box.ledger[key].kind == "sent"), (seed, key, "sent mail mislabelled")
        if step == "tick":
            for key in box.promised(ctx):
                assert key in seen, (seed, key, "covered by the watermark but never seen")

    fake.disarm()
    knobs["cap"] = None
    fake.world.health_override.clear()
    for _ in range(150):
        owner_says_yes(ctx)
        run_tick(ctx, clock)
        state = mark(ctx)
        if (
            {key for key, _ in rows_added} == box.wanted(ctx)
            and state["gap_from"] is None
            and state["held_from"] is None
        ):
            break
    state = mark(ctx)
    seen = {key for key, _ in rows_added}
    assert len(seen) == len(rows_added), (seed, "a message was stored twice")
    assert state["gap_from"] is None, (seed, dict(state))
    assert state["held_from"] is None, (seed, dict(state))
    wanted = box.wanted(ctx)
    assert seen == wanted, (seed, sorted(wanted - seen)[:5], sorted(seen - wanted)[:5])
    assert "source.stale" not in kinds(ctx)
    waiting = sum(b.count for b in repo.mail_batches(ctx.db) if not b.sent)
    assert waiting == len(keys_seen_in(ctx)), "sent mail is not new mail"


def test_a_mail_read_on_the_phone_is_still_seen_once(
    ctx: Context, fake: FakeProvider, clock: ManualClock, seed_sources: None
) -> None:
    fake.mail.add_message(SOURCE, internet_message_id="<a@x>", unread=False)
    run_tick(ctx, clock)
    assert set(keys_seen(ctx)) == {"a@x"}
    run_tick(ctx, clock)
    assert len(keys_seen(ctx)) == 1


def test_a_a_never_watched_source_starts_a_recorded_lookback_back_and_not_further(
    ctx: Context, fake: FakeProvider, clock: ManualClock, seed_sources: None
) -> None:
    fake.mail.add_message(
        SOURCE, internet_message_id="<in@x>", date=clock.now() - timedelta(hours=40)
    )
    fake.mail.add_message(
        SOURCE, internet_message_id="<out@x>", date=clock.now() - timedelta(hours=60)
    )
    run_tick(ctx, clock)
    assert set(keys_seen(ctx)) == {"in@x"}
    assert mark(ctx)["watching_since"] == clock.now().strftime(STAMP)
    assert mark(ctx)["held_from"] is None, "the first look back is not a question"


def test_a_an_unreachable_source_raises_stale_with_the_exact_span_since_the_watermark(
    ctx: Context, fake: FakeProvider, clock: ManualClock, seed_sources: None
) -> None:
    run_tick(ctx, clock)
    through = mark(ctx)["covered_through"]
    fake.world.health_override[SOURCE] = HealthState.UNAVAILABLE
    clock.advance(hours=5)
    run_tick(ctx, clock)
    assert mark(ctx)["covered_through"] == through
    start = through.replace("T", " ")[:16]
    end = clock.now().strftime("%Y-%m-%d %H:%M")
    summary = ctx.db.execute("SELECT summary FROM events WHERE kind = 'source.stale'").fetchone()
    assert summary is not None
    assert f"mail from {start} to {end} UTC is not covered yet" in summary["summary"]
    del fake.world.health_override[SOURCE]
    run_tick(ctx, clock)
    assert mark(ctx)["gap_from"] is None
    assert "source.stale" not in kinds(ctx)
