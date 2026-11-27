"""The fallback delivery check: a second opinion when the normal freshness check says `stale`.

Contract: docs/contracts/07-attention.md (A-7) and 08-ports.md (P-5). It never runs routinely:
only for a daily mailbox whose health came back `stale` (never `unavailable`), at most once per
mailbox per 3 hours, only in daily operation (not the live test's own app), and only with the
owner's standing grant (`writes/delivery_check.py`). It sends a marked message from the mailbox
to itself, waits for it to appear in the Inbox, then moves every copy to Trash. Both writes go
through the write engine, so they are audited and exactly-once; a durable record (one
`system_state` JSON, one entry per mailbox) is written before the send, so recovery after a
crash only cleans up and never sends again. It never raises into the tick or `start`.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from chamberlain.app.context import Context
from chamberlain.attention import repo, sync
from chamberlain.domain.capabilities import HealthState, SourceHealth
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import MailSend, MailTrash
from chamberlain.domain.mail import FolderRole, MailQuery, MessageSummary
from chamberlain.domain.operations import OperationState
from chamberlain.domain.testmark import DELIVERY_MARK, is_delivery_check
from chamberlain.domain.timefmt import parse, ts
from chamberlain.ports.health import active_health_checks_allowed
from chamberlain.ports.sync import FolderSync
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction
from chamberlain.store.schema import DELIVERY_CHECK_KEY, get_state, set_state
from chamberlain.writes import delivery_check as grants
from chamberlain.writes.core import Engine
from chamberlain.writes.repo import own_addresses

log = logging.getLogger("chamberlain.attention")

PROBE_EVERY = timedelta(hours=3)
"""At most one probe per mailbox in this time, whatever its outcome."""
POLL_SECONDS = 5.0
SYNC_SECONDS = 30.0
CLEANUP_PASSES = 3
"""Trashing one copy can remove or merge another (as on Gmail): list again, a few passes."""
FAILED_NOTE_FOR = timedelta(hours=6)
"""How long a failed check is still named in the report's access-problem line."""

GIVE_UP_AFTER = timedelta(hours=24)
"""A message that never turned up in the Inbox stops being looked for after this."""

sleep: Callable[[float], None] = time.sleep
"""The wait between arrival polls (tests replace it)."""


@dataclass(frozen=True)
class Record:
    """What is durably known about one mailbox's last delivery check."""

    last_at: str | None = None
    """When the last probe started: the rate limit."""
    pending: str | None = None
    """The subject of a probe message not yet confirmed removed: recovery only cleans up."""
    result: str | None = None
    """`arrived`, `missing` (did not arrive in time) or `not_sent`."""
    result_at: str | None = None
    """For `arrived`, the arrival time: the `fresh_as_of` the check vouched for."""

    def stored(self) -> dict[str, str]:
        return {k: v for k, v in vars(self).items() if v}


def records(db: sqlite3.Connection) -> dict[str, Record]:
    """Every mailbox's record (the report and the doctor read it); empty when unreadable."""
    raw = get_state(db, DELIVERY_CHECK_KEY)
    found: dict[str, Record] = {}
    try:
        data = cast(dict[str, dict[str, object]], json.loads(raw) if raw else {})
        names = set(Record.__dataclass_fields__)
        for source, item in data.items():
            found[str(source)] = Record(**{k: str(v) for k, v in item.items() if k in names})
    except (ValueError, TypeError, AttributeError):
        log_failure(log, "the delivery-check record could not be read")
    return found


def _save(ctx: Context, source: str, record: Record) -> None:
    """Replace one mailbox's entry; the caller holds a transaction when it also reads."""
    data = {s: r.stored() for s, r in records(ctx.db).items()}
    data[source] = record.stored()
    set_state(ctx.db, ctx.clock, DELIVERY_CHECK_KEY, json.dumps(data, sort_keys=True))


def confirm(ctx: Context, source: str, health: SourceHealth) -> SourceHealth:
    """`health`, or `ok` when the fallback check saw the message arrive. Never raises."""
    try:
        wait = sync.settings(ctx)
        if (
            not ctx.sources.hide_test_marked
            or source not in ctx.sources.daily_mail()
            or not wait.delivery_check
            or not active_health_checks_allowed()
        ):
            return health
        _sweep(ctx, source)
        if health.state is not HealthState.STALE:
            return health
        return _probe(ctx, source, health, wait.delivery_seconds)
    except Exception:
        log_failure(log, "the delivery check failed for %s", source)
        return health


def _probe(ctx: Context, source: str, health: SourceHealth, limit: timedelta) -> SourceHealth:
    scope = grants.scope_for(ctx, source)
    address = grants.own_address(ctx.db, source)
    if scope is None or address is None:
        return health
    grant = grants.active_grant(ctx, scope)
    if grant is None:
        grants.propose_missing(ctx, scope)  # once; never sent without the owner's click
        return health
    started = ctx.clock.now()
    with transaction(ctx.db):
        before = records(ctx.db).get(source, Record())
        if before.last_at and started - parse(before.last_at) < PROBE_EVERY:
            return _held(ctx, source, health, before)
        _save(ctx, source, Record(ts(started), before.pending, before.result, before.result_at))
    engine = Engine(ctx)
    if before.pending and not _clean(ctx, engine, source, grant.handle)[0]:
        log.info("The earlier delivery-check message in %s is still not in Trash", source)
        return health  # recovery only cleans up: never another send on top of it
    subject = f"{DELIVERY_MARK} {source} {started:%Y%m%dT%H%M%SZ}"
    _save(ctx, source, Record(ts(started), subject, before.result, before.result_at))
    intent = MailSend(
        source=source,
        to=(address,),
        subject=subject,
        body=(
            f"This is an automatic Chamberlain delivery check for {source}. It was sent because "
            "the usual freshness check failed, and it is moved to Trash once it arrives. "
            "You can ignore it."
        ),
    )
    sent = engine.run(intent, actor=grants.ACTOR, grant=grant.handle)
    if sent is None or sent.state is not OperationState.CONFIRMED:
        log.info("The delivery-check message from %s was not confirmed as sent", source)
        _save(ctx, source, Record(ts(started), subject, "not_sent", ts(ctx.clock.now())))
        return health
    arrived = _await(ctx, source, subject, started + limit)
    now = ts(ctx.clock.now())
    cleaned, _ = _clean(ctx, engine, source, grant.handle)
    keep = None if arrived and cleaned else subject  # a late arrival is swept at a later tick
    if arrived is None:
        _save(ctx, source, Record(ts(started), keep, "missing", now))
        return health
    _save(ctx, source, Record(ts(started), keep, "arrived", ts(arrived)))
    repo.write_health(ctx.db, source, HealthState.OK, now, ts(arrived))
    return health.model_copy(update={"state": HealthState.OK, "fresh_as_of": arrived})


def _sweep(ctx: Context, source: str) -> None:
    """At every health check while a probe message may still be about (it arrived after the
    wait, or its cleanup failed): move it to Trash. Searches only, unless something is found."""
    record = records(ctx.db).get(source, Record())
    if not record.pending:
        return
    scope = grants.scope_for(ctx, source)
    grant = grants.active_grant(ctx, scope) if scope is not None else None
    if grant is None:
        return
    cleaned, inbox = _clean(ctx, Engine(ctx), source, grant.handle)
    stamp = record.result_at or record.last_at
    expired = stamp is not None and ctx.clock.now() - parse(stamp) > GIVE_UP_AFTER
    if cleaned and (inbox or record.result == "arrived" or expired):
        with transaction(ctx.db):
            current = records(ctx.db).get(source, Record())
            if current.pending == record.pending:
                _save(ctx, source, Record(current.last_at, None, current.result, current.result_at))


def _held(ctx: Context, source: str, health: SourceHealth, record: Record) -> SourceHealth:
    """Between probes, a check that saw its message arrive still vouches for the mailbox: `ok`,
    fresh as of that arrival (never later), so one busy moment is not an access problem."""
    if record.result != "arrived" or not record.result_at or record.pending:
        return health
    arrived = parse(record.result_at)
    repo.write_health(ctx.db, source, HealthState.OK, ts(ctx.clock.now()), record.result_at)
    return health.model_copy(update={"state": HealthState.OK, "fresh_as_of": arrived})


def _marked(
    ctx: Context, source: str, role: FolderRole, subject: str | None = None
) -> list[MessageSummary]:
    """This mailbox's delivery-check copies in every folder of `role` (Exchange can show two
    Sent folders, as KCL does: "Sent" and "Sent Items")."""
    port = ctx.sources.mail_port(source)
    own = own_addresses(ctx.db, source)
    since = ctx.clock.now() - GIVE_UP_AFTER - timedelta(hours=1)
    items: list[MessageSummary] = []
    for folder in grants.folders_of(port.folders(source), role):
        # A listing of recent mail, matched here: Thunderbird's text search misses mail that
        # has just arrived (found live on KCL, 2026-11-27).
        query = MailQuery(folder_id=folder, since=since, limit=200)
        items.extend(port.search(source, query).items)
    return [
        m
        for m in items
        if is_delivery_check(m.subject, m.sender.address, own)
        and f" {source} " in m.subject
        and (subject is None or m.subject.strip() == subject)
    ]


def _await(ctx: Context, source: str, subject: str, deadline: datetime) -> datetime | None:
    """The time the message was first seen in the Inbox, or None when it did not come in time."""
    port = ctx.sources.mail_port(source)
    inbox = grants.folders_of(port.folders(source), FolderRole.INBOX)
    polls = int(max(0.0, (deadline - ctx.clock.now()).total_seconds()) / POLL_SECONDS) + 2
    for _ in range(polls):
        left = deadline - ctx.clock.now()
        if inbox and isinstance(port, FolderSync) and left > timedelta(0):
            try:
                port.sync_folders(source, inbox, min(left, timedelta(seconds=SYNC_SECONDS)))
            except Exception:
                log_failure(log, "syncing the Inbox of %s for the delivery check failed", source)
        try:
            if _marked(ctx, source, FolderRole.INBOX, subject):
                return ctx.clock.now()
        except Exception:
            log_failure(log, "looking for the delivery-check message in %s failed", source)
        left = deadline - ctx.clock.now()
        if left <= timedelta(0):
            return None
        sleep(min(POLL_SECONDS, left.total_seconds()))
    return None


def _movable(found: list[MessageSummary]) -> list[MessageSummary]:
    """The copies the write engine can move. Two copies of one message in one folder (Exchange
    saving its own Sent copy beside Thunderbird's) are never moved: the engine refuses to choose
    between them. They stay where they are, logged, and do not count as left over."""
    seen: dict[tuple[str, str], int] = {}
    for m in found:
        key = (m.folder_id, m.internet_message_id)
        seen[key] = seen.get(key, 0) + 1
    twins = [m for m in found if seen[(m.folder_id, m.internet_message_id)] > 1]
    if twins:
        log.info("%d duplicate delivery-check copies stay in their folder", len(twins))
    return [m for m in found if seen[(m.folder_id, m.internet_message_id)] == 1]


def _clean(ctx: Context, engine: Engine, source: str, grant: str) -> tuple[bool, bool]:
    """Move every copy of this mailbox's delivery-check mail (Inbox, Sent) to Trash.

    One `mail.trash` write per folder per pass. Returns (none is left, an Inbox copy was moved).
    A failure is logged and left for the next sweep (the record still names the message)."""
    inbox_moved = False
    try:
        for _ in range(CLEANUP_PASSES):
            inbox = _marked(ctx, source, FolderRole.INBOX)
            found = _movable(inbox + _marked(ctx, source, FolderRole.SENT))
            if not found:
                return True, inbox_moved
            by_folder: dict[str, list[MessageSummary]] = {}
            for message in found:
                by_folder.setdefault(message.folder_id, []).append(message)
            progress = False
            for copies in by_folder.values():
                handles = tuple(
                    dict.fromkeys(
                        ctx.services.refs.remember(
                            HandleKind.MESSAGE, source, c.locator, c.internet_message_id
                        )
                        for c in copies
                    )
                )
                done = engine.run(
                    MailTrash(source=source, messages=handles), actor=grants.ACTOR, grant=grant
                )
                moved = done is not None and done.state is OperationState.CONFIRMED
                progress = progress or moved
                inbox_moved = inbox_moved or (moved and copies[0] in inbox)
            if not progress:
                return False, inbox_moved
        left = _marked(ctx, source, FolderRole.INBOX) + _marked(ctx, source, FolderRole.SENT)
        return not _movable(left), inbox_moved
    except Exception:
        log_failure(log, "moving the delivery-check message of %s to Trash failed", source)
        return False, inbox_moved
