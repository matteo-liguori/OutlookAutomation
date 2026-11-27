"""Per-source new-mail watermark over every folder: each message is seen once, or a gap is named.

Contract: docs/contracts/07-attention.md (A-6, A-7, A-20 to A-23). A scan lists the whole mailbox
(every folder except Trash, Junk and Drafts) from a little before the watermark, oldest first, in
time slices that shrink until each one is read to the end. The watermark moves only over slices
that were read completely; a scan that cannot finish keeps what it found and records the span still
uncovered (`source.stale`). One message is one item, whatever folders hold copies of it. Mail the
owner sent is learned from and offered as context, never as new mail. A span more than a day
behind is read for the last day only; the older part waits for the owner's word. Nothing here
changes remote state (P-3) or stores a sender or subject.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from chamberlain.app.context import Context
from chamberlain.attention import repo, sync
from chamberlain.attention.suggest import Suggester
from chamberlain.attention.tuning import (
    MAIL_BOOTSTRAP,
    MAIL_ESTIMATE_PAGES,
    MAIL_MAX_PAGES,
    MAIL_OVERLAP,
    MAIL_PAGE_LIMIT,
    MAIL_REQUIRED_OVERLAP,
    MAIL_SENT_KEEP,
    MAIL_SKIPPED_ROLES,
    MAIL_STANDARD,
)
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.mail import FolderRole, MailQuery, MessageSummary, message_key
from chamberlain.domain.testmark import carries_delivery_mark
from chamberlain.domain.timefmt import parse, ts
from chamberlain.memory.refs import existing_handles, remember_many
from chamberlain.ports.mail import MailPort
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction

log = logging.getLogger("chamberlain.attention")

_MIN_SLICE = timedelta(seconds=1)


@dataclass
class _Walk:
    """How far one oldest-first walk got."""

    frontier: datetime
    width: int | None
    clean: bool = True
    """False after a failure or a gap the walk could not narrow away."""


Take = Callable[[list[MessageSummary]], None]


def _walk(
    port: MailPort,
    source: str,
    lo: datetime,
    hi: datetime,
    width: int | None,
    take: Take,
    folder_id: str | None = None,
) -> _Walk:
    """Read [lo, hi] oldest slice first. Items are handed to `take` even from a slice that did not
    finish; the frontier moves only over slices read to the end with no gap (A-20).

    A slice that needs more pages than the budget allows, or that the connector says to narrow,
    is halved. The width is kept so the next tick starts from a slice that fits.
    """
    walk = _Walk(lo, width)
    pages_left = MAIL_MAX_PAGES
    while walk.frontier < hi and pages_left > 0:
        span = hi - walk.frontier
        step = span if walk.width is None else min(span, timedelta(seconds=walk.width))
        slice_end = walk.frontier + step
        last = slice_end >= hi
        # Timestamps are whole seconds (D-7): take the whole last second of the slice.
        query = MailQuery(
            since=walk.frontier,
            until=slice_end + _MIN_SLICE,
            limit=MAIL_PAGE_LIMIT,
            folder_id=folder_id,
        )
        cursor: str | None = None
        used = 0
        finished = other = False
        while pages_left > 0:
            page = port.search(source, query, cursor)
            pages_left -= 1
            used += 1
            take(list(page.items))
            if page.gaps:
                other = not any(g.narrow for g in page.gaps)
                break
            if page.cursor is None:
                finished = True
                break
            cursor = page.cursor
        if finished:
            walk.frontier = hi if last else slice_end
            if walk.width is not None and used == 1:
                walk.width = None if walk.width * 2 >= span.total_seconds() else walk.width * 2
            continue
        if other or step <= _MIN_SLICE:
            walk.clean = False
            return walk
        walk.width = max(1, int(step.total_seconds()) // 2)
    return walk


def _estimate(
    port: MailPort, source: str, skipped: frozenset[str], lo: datetime, hi: datetime
) -> int | None:
    """About how many messages a span holds: a few pages counted, the rest extrapolated by time.

    Reads only headers, newest first, and never pages through the whole span.
    """
    keys: set[str] = set()
    cursor: str | None = None
    oldest = hi
    try:
        for _ in range(MAIL_ESTIMATE_PAGES):
            page = port.search(source, MailQuery(since=lo, until=hi, limit=MAIL_PAGE_LIMIT), cursor)
            for item in page.items:
                if item.folder_id not in skipped:
                    keys.add(message_key(item.internet_message_id, item.locator))
                oldest = min(oldest, item.date)
            if page.cursor is None:
                return len(keys)
            cursor = page.cursor
    except Exception:
        log_failure(log, "estimating older mail failed for %s", source)
        return None
    covered = max((hi - oldest).total_seconds(), 1.0)
    return round(len(keys) * (hi - lo).total_seconds() / covered)


def scan(ctx: Context, source: str, health: HealthState | None, suggester: Suggester) -> bool:
    """True when the whole range was covered and the watermark moved."""
    now = ctx.clock.now().replace(microsecond=0)
    state = repo.watermark(ctx.db, source)
    first = state is None
    if state is None:
        with transaction(ctx.db):
            repo.begin_watching(ctx.db, source, ts(now - MAIL_BOOTSTRAP + MAIL_OVERLAP), ts(now))
        state = repo.watermark(ctx.db, source)
        if state is None:
            return False
    through = parse(state.covered_through)
    fresh = state.scan_started_at is None
    started = now if fresh else parse(state.scan_started_at or ts(now))
    resume = parse(state.scan_from) if state.scan_from else None

    if health in (HealthState.UNAVAILABLE, HealthState.SIGN_IN_NEEDED):
        begin = max(through, resume or through)
        _record(ctx, source, state, (ts(begin), ts(max(now, begin))))
        return False

    sync.ensure_fresh(ctx, source)
    window = _window(now, through, started, resume, state.held, first=first, fresh=fresh)
    read = _read(ctx, source, health, window, state.scan_width)
    plan = _plan(ctx, source, health, window, read, suggester)
    return _commit(ctx, source, window, plan)


@dataclass(frozen=True)
class _Window:
    now: datetime
    through: datetime
    started: datetime
    resume: datetime | None
    frontier: datetime
    held: repo.Held | None
    hold_from: datetime | None
    look_for_late_arrivals: bool


def _window(
    now: datetime,
    through: datetime,
    started: datetime,
    resume: datetime | None,
    held: repo.Held | None,
    *,
    first: bool,
    fresh: bool,
) -> _Window:
    hold_from: datetime | None = None
    if fresh and now - through > MAIL_STANDARD:
        # Read the last day and hold the older part for the owner (A-23).
        hold_from = through - MAIL_REQUIRED_OVERLAP
        if held is not None:
            hold_from = min(hold_from, parse(held.start))
        through = now - MAIL_STANDARD
    frontier = now - MAIL_BOOTSTRAP if first else resume or through - MAIL_REQUIRED_OVERLAP
    if hold_from is not None:
        frontier = max(frontier, through)
    elif held is not None:
        frontier = max(frontier, parse(held.end))
    return _Window(now, through, started, resume, frontier, held, hold_from, fresh and not first)


@dataclass(frozen=True)
class _Read:
    port: MailPort | None
    copies: dict[str, list[MessageSummary]]
    roles: dict[str, FolderRole]
    walked: _Walk
    held: repo.Held | None
    caught_up: dict[str, _Walk]


def _read(
    ctx: Context, source: str, health: HealthState | None, window: _Window, width: int | None
) -> _Read:
    """Read the main window, approved older mail and recovered folders."""
    through, started, held, hold_from = (
        window.through,
        window.started,
        window.held,
        window.hold_from,
    )
    found: dict[str, list[MessageSummary]] = {}
    roles: dict[str, FolderRole] = {}
    walked = _Walk(window.frontier, width)
    estimate: int | None = None
    catch_up: _Walk | None = None
    catch_ups: dict[str, _Walk] = {}
    stuck_hold: repo.Held | None = None
    port: MailPort | None = None
    try:
        port = ctx.sources.mail_port(source)
        roles = {f.id: f.role for f in port.folders(source)}
        skipped = frozenset(i for i, role in roles.items() if role in MAIL_SKIPPED_ROLES)

        def take(items: list[MessageSummary]) -> None:
            for item in items:
                if carries_delivery_mark(item.subject):
                    continue  # the fallback delivery check's own message: never mail to handle
                if item.folder_id not in skipped:
                    key = message_key(item.internet_message_id, item.locator)
                    found.setdefault(key, []).append(item)

        if window.look_for_late_arrivals:
            # Late arrivals dated up to a day before the watermark: best effort, one page.
            early = through - MAIL_OVERLAP
            if early < window.frontier:
                page = port.search(
                    source, MailQuery(since=early, until=window.frontier, limit=MAIL_PAGE_LIMIT)
                )
                take(list(page.items))
        if hold_from is not None:
            estimate = _estimate(port, source, skipped, hold_from, through)
        walked = _walk(port, source, window.frontier, started, width, take)
        current = held
        if hold_from is not None:
            current = repo.Held(ts(hold_from), ts(through), "asking", estimate, None)
        if (
            current is not None
            and current.state == "approved"
            and walked.clean
            and health is HealthState.OK
        ):
            try:
                catch_up = _walk(
                    port,
                    source,
                    parse(current.start),
                    parse(current.end),
                    current.width,
                    take,
                )
            except Exception:
                log_failure(log, "reading older mail failed for %s", source)
        catch_ups, unread_from = _catch_up(ctx, port, source, started, take)
        if unread_from is not None:
            stuck_hold = _stuck_hold(
                port,
                source,
                skipped,
                _held_after(held, hold_from, through, estimate, None),
                unread_from,
                started - MAIL_STANDARD,
            )
    except Exception:
        log_failure(log, "mail scan failed for %s", source)
        walked.clean = False

    held_now = stuck_hold or _held_after(held, hold_from, through, estimate, catch_up)
    return _Read(port, found, roles, walked, held_now, catch_ups)


@dataclass(frozen=True)
class _Seen:
    key: str
    locator: str
    message_id: str | None
    received: str
    card: str | None
    sent: bool


@dataclass(frozen=True)
class _Plan:
    complete: bool
    held: repo.Held | None
    rows: list[_Seen]
    learned: set[str]
    catchups: dict[str, str | None]
    frontier: str | None
    width: int | None
    gap: tuple[str, str] | None


def _plan(
    ctx: Context,
    source: str,
    health: HealthState | None,
    window: _Window,
    read: _Read,
    suggester: Suggester,
) -> _Plan:
    """Pick cards and calculate progress before locking the store for the scan commit."""
    rows, learned = _plan_messages(ctx, source, read, suggester)
    complete = (
        read.walked.clean and health is HealthState.OK and read.walked.frontier >= window.started
    )
    catchups = {
        folder: (
            None if walked.clean and walked.frontier >= window.started else ts(walked.frontier)
        )
        for folder, walked in read.caught_up.items()
    }
    if complete:
        return _Plan(True, read.held, rows, learned, catchups, None, None, None)
    # When health remains stale, keep the old frontier and name only uncovered newer mail.
    frontier = read.walked.frontier
    if health is not HealthState.OK:
        frontier = window.resume or frontier
    begin = max(frontier, window.through)
    gap = (ts(begin), ts(max(window.now, begin)))
    return _Plan(False, read.held, rows, learned, catchups, ts(frontier), read.walked.width, gap)


def _plan_messages(
    ctx: Context, source: str, read: _Read, suggester: Suggester
) -> tuple[list[_Seen], set[str]]:
    port = read.port
    if port is None or not read.copies:
        return [], set()
    known = repo.seen_keys(ctx.db, source, list(read.copies))
    new = {key: copies for key, copies in read.copies.items() if key not in known}
    chosen = {
        key: next((c for c in copies if read.roles.get(c.folder_id) is FolderRole.INBOX), copies[0])
        for key, copies in new.items()
    }
    handles = existing_handles(
        ctx.db, HandleKind.MESSAGE, source, (c.locator for c in chosen.values())
    )
    own = ctx.services.writes.own_addresses(source)
    rows: list[_Seen] = []
    learned: set[str] = set()
    for key, message in chosen.items():
        sent_copies = [c for c in new[key] if read.roles.get(c.folder_id) is FolderRole.SENT]
        sent = read.roles.get(message.folder_id) is not FolderRole.INBOX and (
            bool(sent_copies) or message.sender.address.lower() in own
        )
        handle = handles.get(message.locator)
        if sent:
            card = suggester.suggest_sent(port, source, handle, message)
            # Only a Sent folder copy teaches recipients (G-15). Anyone can forge a From header.
            learned.update(a.address for copy in sent_copies for a in (*copy.to, *copy.cc))
        else:
            card = suggester.suggest(port, source, handle, message)
        rows.append(
            _Seen(key, message.locator, message.internet_message_id, ts(message.date), card, sent)
        )
    return rows, learned


def _commit(ctx: Context, source: str, window: _Window, plan: _Plan) -> bool:
    with transaction(ctx.db):
        handles = remember_many(
            ctx.db,
            ctx.clock,
            HandleKind.MESSAGE,
            source,
            ((row.locator, row.message_id) for row in plan.rows),
        )
        for row in plan.rows:
            repo.add_seen(
                ctx.db,
                source,
                row.key,
                row.locator,
                handles[row.locator],
                row.received,
                ts(window.now),
                row.card,
                sent=row.sent,
            )
        if plan.learned:
            ctx.services.writes.learn_recipients(source, plan.learned)
        if plan.held != window.held:
            repo.save_held(ctx.db, source, plan.held)
        for folder, frontier in plan.catchups.items():
            repo.set_catchup(ctx.db, source, folder, frontier)
        repo.save_scan(
            ctx.db,
            source,
            through=ts(window.started if plan.complete else window.through),
            started=None if plan.complete else ts(window.started),
            frontier=plan.frontier,
            width=plan.width,
            gap=plan.gap,
        )
        if plan.complete:
            boundary = ts(window.started - MAIL_OVERLAP)
            if plan.held is not None:
                boundary = min(boundary, plan.held.start)
            repo.prune_seen(ctx.db, source, boundary, ts(window.now - MAIL_SENT_KEEP))
    return plan.complete


def _stuck_hold(
    port: MailPort,
    source: str,
    skipped: frozenset[str],
    current: repo.Held | None,
    unread_from: datetime,
    cut: datetime,
) -> repo.Held | None:
    """The older-mail span to ask about for mail a stuck folder kept back more than a day (A-27).

    None when the question already covers it (asked, answered or declined). Otherwise the
    span grows to take in `[unread_from, cut)` and the owner is asked again.
    """
    if current is not None and parse(current.start) <= unread_from:
        return None
    end = cut if current is None else max(cut, parse(current.end))
    return repo.Held(
        ts(unread_from),
        ts(end),
        "asking",
        _estimate(port, source, skipped, unread_from, end),
        None,
    )


def _catch_up(
    ctx: Context, port: MailPort, source: str, started: datetime, take: Take
) -> tuple[dict[str, _Walk], datetime | None]:
    """Read, on its own, each folder that failed to sync and has synced again (A-27).

    While a folder would not sync, mail may have reached it dated before the watermark moved on;
    the ordinary window would never look there again. Each such folder is read from its last good
    sync (an hour earlier, at most a day back) to now. A walk that does not finish keeps its
    place for the next tick.

    Also returns where the oldest folder's unread span begins when it is more than a day back:
    that older mail is not read here, it is asked about like the whole-mailbox case (A-23).
    """
    found: dict[str, _Walk] = {}
    unread_from: datetime | None = None
    for row in repo.sync_folder_rows(ctx.db, source):
        if row.catchup_from is None or row.failure:
            continue
        wanted = parse(row.catchup_from) - MAIL_REQUIRED_OVERLAP
        lo = max(wanted, started - MAIL_STANDARD)
        if wanted < lo and (unread_from is None or wanted < unread_from):
            unread_from = wanted
        try:
            found[row.folder_id] = _walk(port, source, lo, started, None, take, row.folder_id)
        except Exception:
            log_failure(log, "reading folder %s of %s failed", row.folder_id, source)
    return found, unread_from


def _held_after(
    held: repo.Held | None,
    hold_from: datetime | None,
    cut: datetime,
    estimate: int | None,
    catch_up: _Walk | None,
) -> repo.Held | None:
    """The held span once this tick is done: new, extended, advanced by the catch-up, or gone."""
    if hold_from is not None:
        return repo.Held(ts(hold_from), ts(cut), "asking", estimate, None)
    if held is None or catch_up is None:
        return held
    if catch_up.frontier >= parse(held.end):
        return None
    return repo.Held(ts(catch_up.frontier), held.end, held.state, held.estimate, catch_up.width)


def _record(ctx: Context, source: str, state: repo.Watermark, gap: tuple[str, str]) -> None:
    with transaction(ctx.db):
        repo.save_scan(
            ctx.db,
            source,
            through=state.covered_through,
            started=state.scan_started_at,
            frontier=state.scan_from,
            width=state.scan_width,
            gap=gap,
        )
