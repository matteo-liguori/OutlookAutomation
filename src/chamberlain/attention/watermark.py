"""Per-source new-mail watermark: every message is seen once, or a gap is named.

Contract: docs/contracts/07-attention.md (A-6, A-7). A scan lists the inbox from a
little before the watermark, whatever the read state, pages until the connector says it
is complete, and only then moves the watermark. A scan that cannot finish keeps what it
found, remembers how far it got, and records the span still uncovered (`source.stale`).
Nothing here changes remote state (P-3) or stores a sender or subject.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from chamberlain.app.context import Context
from chamberlain.attention import repo
from chamberlain.attention.suggest import Suggester
from chamberlain.attention.tuning import (
    MAIL_BOOTSTRAP,
    MAIL_MAX_PAGES,
    MAIL_OVERLAP,
    MAIL_PAGE_LIMIT,
    MAIL_REQUIRED_OVERLAP,
)
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.mail import FolderRole, MailQuery, MessageSummary
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction

log = logging.getLogger("chamberlain.attention")

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _fmt(when: datetime) -> str:
    return when.strftime(_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _FORMAT).replace(tzinfo=UTC)


@dataclass(frozen=True)
class Scanned:
    complete: bool
    """True when the whole range was covered and the watermark moved."""


def scan(ctx: Context, source: str, health: HealthState | None, suggester: Suggester) -> Scanned:
    now = ctx.clock.now()
    state = repo.watermark(ctx.db, source)
    if state is None:
        with transaction(ctx.db):
            repo.begin_watching(
                ctx.db, source, _fmt(now - MAIL_BOOTSTRAP + MAIL_OVERLAP), _fmt(now)
            )
        state = repo.watermark(ctx.db, source)
        if state is None:
            return Scanned(False)
    through = _parse(state.covered_through)
    since = through - MAIL_OVERLAP
    started = _parse(state.scan_started_at) if state.scan_started_at else now
    until = _parse(state.scan_until) if state.scan_until else None
    frontier = until if until is not None else started

    if health in (HealthState.UNAVAILABLE, HealthState.SIGN_IN_NEEDED):
        _record(
            ctx,
            source,
            state.covered_through,
            state.scan_started_at,
            state.scan_until,
            (_fmt(through), _fmt(max(frontier, through))),
        )
        return Scanned(False)

    found: list[MessageSummary] = []
    failed = False
    exhausted = False
    gapped = False
    try:
        port = ctx.sources.mail_port(source)
        inbox = [f for f in port.folders(source) if f.role is FolderRole.INBOX]
        if not inbox:
            raise LookupError("no inbox")
        query = MailQuery(
            folder_id=inbox[0].id,
            since=since,
            # Timestamps are whole seconds (D-7): include the whole frontier second.
            until=None if until is None else until + timedelta(seconds=1),
            limit=MAIL_PAGE_LIMIT,
        )
        cursor: str | None = None
        for _ in range(MAIL_MAX_PAGES):
            page = port.search(source, query, cursor)
            found.extend(page.items)
            if page.gaps:
                gapped = True
                break
            if page.cursor is None:
                exhausted = True
                break
            cursor = page.cursor
    except Exception:
        log_failure(log, "mail scan failed for %s", source)
        failed = True

    # Connector round trips (reply headers, correspondents) happen before the write lock.
    rows: list[tuple[str, str, str, str | None]] = []
    if found:
        port = ctx.sources.mail_port(source)
        for item in found:
            if repo.is_seen(ctx.db, source, item.locator):
                continue
            handle = ctx.services.refs.remember(
                HandleKind.MESSAGE, source, item.locator, item.internet_message_id
            )
            card = suggester.suggest(port, source, handle, item)
            rows.append((item.locator, handle, _fmt(item.date), card))

    with transaction(ctx.db):
        for locator, handle, received, card in rows:
            repo.add_seen(ctx.db, source, locator, handle, received, _fmt(now), card)

        # The overlap below the watermark only catches late arrivals. Once a page reaches the
        # required part of it, the scan is complete enough for a mailbox bigger than one tick.
        reached = bool(found) and min(i.date for i in found) <= through - MAIL_REQUIRED_OVERLAP
        if (exhausted or reached) and not failed and not gapped and health is HealthState.OK:
            moved = max(through, started)
            repo.save_scan(ctx.db, source, through=_fmt(moved), started=None, until=None, gap=None)
            repo.prune_seen(ctx.db, source, _fmt(moved - MAIL_OVERLAP))
            return Scanned(True)

        # Not finished: keep how far a sound scan got. Pages come newest first, so the oldest
        # date reached is a frontier, except after a connector gap, where nothing is promised.
        keep_started = state.scan_started_at
        keep_until = state.scan_until
        if gapped:
            pass
        elif exhausted:
            keep_started = keep_until = None
        elif found:
            keep_started, keep_until = _fmt(started), _fmt(min(i.date for i in found))
        end = _parse(keep_until) if keep_until else (frontier if not exhausted else now)
        # Only mail newer than the watermark can be unseen; rescanning the overlap is not a gap.
        gap = (_fmt(through), _fmt(end)) if end > through else None
        repo.save_scan(
            ctx.db,
            source,
            through=state.covered_through,
            started=keep_started,
            until=keep_until,
            gap=gap,
        )
    return Scanned(False)


def _record(
    ctx: Context,
    source: str,
    through: str,
    started: str | None,
    until: str | None,
    gap: tuple[str, str],
) -> None:
    with transaction(ctx.db):
        repo.save_scan(ctx.db, source, through=through, started=started, until=until, gap=gap)
