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
from datetime import UTC, datetime, timedelta

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
from chamberlain.ports.mail import MailPort
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction

log = logging.getLogger("chamberlain.attention")

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_MIN_SLICE = timedelta(seconds=1)


def _fmt(when: datetime) -> str:
    return when.strftime(_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _FORMAT).replace(tzinfo=UTC)


@dataclass(frozen=True)
class Scanned:
    complete: bool
    """True when the whole range was covered and the watermark moved."""


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


def scan(ctx: Context, source: str, health: HealthState | None, suggester: Suggester) -> Scanned:
    now = ctx.clock.now().replace(microsecond=0)
    state = repo.watermark(ctx.db, source)
    first = state is None
    if state is None:
        with transaction(ctx.db):
            repo.begin_watching(
                ctx.db, source, _fmt(now - MAIL_BOOTSTRAP + MAIL_OVERLAP), _fmt(now)
            )
        state = repo.watermark(ctx.db, source)
        if state is None:
            return Scanned(False)
    through = _parse(state.covered_through)
    fresh = state.scan_started_at is None
    started = now if fresh else _parse(state.scan_started_at or _fmt(now))
    resume = _parse(state.scan_from) if state.scan_from else None

    if health in (HealthState.UNAVAILABLE, HealthState.SIGN_IN_NEEDED):
        begin = max(through, resume or through)
        _record(ctx, source, state, (_fmt(begin), _fmt(max(now, begin))))
        return Scanned(False)

    # Every folder the scan reads is brought up to date first, and the scan waits for it (A-24).
    # A folder that will not sync is named by `source.stale`; the scan goes on without it.
    sync.ensure_fresh(ctx, source)

    held = state.held
    hold_from: datetime | None = None
    if fresh and now - through > MAIL_STANDARD:
        # More than a day uncovered: read the last day, hold the older part for the owner (A-23).
        hold_from = through - MAIL_REQUIRED_OVERLAP
        if held is not None:
            hold_from = min(hold_from, _parse(held.start))
        through = now - MAIL_STANDARD

    found: dict[str, list[MessageSummary]] = {}
    roles: dict[str, FolderRole] = {}
    failed = False
    width = state.scan_width
    estimate: int | None = None
    catch_up: _Walk | None = None
    catch_ups: dict[str, _Walk] = {}
    port: MailPort | None = None
    frontier = through - MAIL_REQUIRED_OVERLAP
    if first:
        frontier = now - MAIL_BOOTSTRAP
    elif resume is not None:
        frontier = resume
    if hold_from is not None:
        frontier = max(frontier, through)
    elif held is not None:
        frontier = max(frontier, _parse(held.end))
    try:
        port = ctx.sources.mail_port(source)
        roles = {f.id: f.role for f in port.folders(source)}
        skipped = frozenset(i for i, role in roles.items() if role in MAIL_SKIPPED_ROLES)

        def take(items: list[MessageSummary]) -> None:
            for item in items:
                if item.folder_id not in skipped:
                    key = message_key(item.internet_message_id, item.locator)
                    found.setdefault(key, []).append(item)

        if fresh and not first:
            # Late arrivals dated up to a day before the watermark: best effort, one page.
            early = through - MAIL_OVERLAP
            if early < frontier:
                page = port.search(
                    source, MailQuery(since=early, until=frontier, limit=MAIL_PAGE_LIMIT)
                )
                take(list(page.items))
        if hold_from is not None:
            estimate = _estimate(port, source, skipped, hold_from, through)
        walked = _walk(port, source, frontier, started, width, take)
        frontier, width, failed = walked.frontier, walked.width, not walked.clean
        current = held
        if hold_from is not None:
            current = repo.Held(_fmt(hold_from), _fmt(through), "asking", estimate, None)
        if (
            current is not None
            and current.state == "approved"
            and not failed
            and health is HealthState.OK
        ):
            try:
                catch_up = _walk(
                    port,
                    source,
                    _parse(current.start),
                    _parse(current.end),
                    current.width,
                    take,
                )
            except Exception:
                log_failure(log, "reading older mail failed for %s", source)
        catch_ups = _catch_up(ctx, port, source, started, take)
    except Exception:
        log_failure(log, "mail scan failed for %s", source)
        failed = True

    # Connector round trips (reply headers, correspondents) happen before the write lock.
    own = ctx.services.writes.own_addresses(source)
    rows: list[tuple[str, str, str, str, str | None, bool]] = []
    learned: set[str] = set()
    if found and port is not None:
        known = repo.seen_keys(ctx.db, source, list(found))
        for key, copies in found.items():
            if key in known:
                continue
            inbox = [c for c in copies if roles.get(c.folder_id) is FolderRole.INBOX]
            chosen = (inbox or copies)[0]
            sent = not inbox and (
                any(roles.get(c.folder_id) is FolderRole.SENT for c in copies)
                or chosen.sender.address.lower() in own
            )
            handle = ctx.services.refs.remember(
                HandleKind.MESSAGE, source, chosen.locator, chosen.internet_message_id
            )
            if sent:
                card = suggester.suggest_sent(port, source, handle, chosen)
                learned.update(a.address for c in copies for a in (*c.to, *c.cc))
            else:
                card = suggester.suggest(port, source, handle, chosen)
            rows.append((key, chosen.locator, handle, _fmt(chosen.date), card, sent))

    complete = not failed and health is HealthState.OK and frontier >= started
    held_now = _held_after(held, hold_from, through, estimate, catch_up)
    boundary = _fmt(started - MAIL_OVERLAP)
    if held_now is not None:
        boundary = min(boundary, held_now.start)

    with transaction(ctx.db):
        for key, locator, handle, received, card, sent in rows:
            repo.add_seen(
                ctx.db, source, key, locator, handle, received, _fmt(now), card, sent=sent
            )
        if learned:
            ctx.services.writes.learn_recipients(source, learned)
        if held_now != held:
            repo.save_held(ctx.db, source, held_now)
        for folder, walked_folder in catch_ups.items():
            done = walked_folder.clean and walked_folder.frontier >= started
            repo.set_catchup(ctx.db, source, folder, None if done else _fmt(walked_folder.frontier))
        if complete:
            repo.save_scan(
                ctx.db,
                source,
                through=_fmt(started),
                started=None,
                frontier=None,
                width=None,
                gap=None,
            )
            repo.prune_seen(ctx.db, source, boundary, _fmt(now - MAIL_SENT_KEEP))
            return Scanned(True)

        # Not finished: keep how far a sound scan got. A scan held back by health (stale) promises
        # nothing new, so its old frontier stays. Only mail newer than the watermark is a gap.
        keep = frontier if health is HealthState.OK else (resume or frontier)
        begin = max(keep, through)
        repo.save_scan(
            ctx.db,
            source,
            through=_fmt(through),
            started=_fmt(started),
            frontier=_fmt(keep),
            width=width,
            gap=(_fmt(begin), _fmt(max(now, begin))),
        )
    return Scanned(False)


def _catch_up(
    ctx: Context, port: MailPort, source: str, started: datetime, take: Take
) -> dict[str, _Walk]:
    """Read, on its own, each folder that failed to sync and has synced again (A-27).

    While a folder would not sync, mail may have reached it dated before the watermark moved on;
    the ordinary window would never look there again. Each such folder is read from its last good
    sync (an hour earlier, at most a day back) to now. A walk that does not finish keeps its
    place for the next tick.
    """
    found: dict[str, _Walk] = {}
    for row in repo.sync_folder_rows(ctx.db, source):
        if row.catchup_from is None or row.failure:
            continue
        lo = max(_parse(row.catchup_from) - MAIL_REQUIRED_OVERLAP, started - MAIL_STANDARD)
        try:
            found[row.folder_id] = _walk(port, source, lo, started, None, take, row.folder_id)
        except Exception:
            log_failure(log, "reading folder %s of %s failed", row.folder_id, source)
    return found


def _held_after(
    held: repo.Held | None,
    hold_from: datetime | None,
    cut: datetime,
    estimate: int | None,
    catch_up: _Walk | None,
) -> repo.Held | None:
    """The held span once this tick is done: new, extended, advanced by the catch-up, or gone."""
    if hold_from is not None:
        return repo.Held(_fmt(hold_from), _fmt(cut), "asking", estimate, None)
    if held is None or catch_up is None:
        return held
    if catch_up.frontier >= _parse(held.end):
        return None
    return repo.Held(_fmt(catch_up.frontier), held.end, held.state, held.estimate, catch_up.width)


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
