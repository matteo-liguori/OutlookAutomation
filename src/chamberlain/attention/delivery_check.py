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
from chamberlain.domain.testmark import DELIVERY_MARK, carries_delivery_mark
from chamberlain.domain.timefmt import parse, ts
from chamberlain.ports.health import active_health_checks_allowed
from chamberlain.ports.sync import FolderSync
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction
from chamberlain.store.schema import DELIVERY_CHECK_KEY, get_state, set_state
from chamberlain.writes import delivery_check as grants
from chamberlain.writes.core import Engine

log = logging.getLogger("chamberlain.attention")

PROBE_EVERY = timedelta(hours=3)
"""At most one probe per mailbox in this time, whatever its outcome."""
POLL_SECONDS = 5.0
SYNC_SECONDS = 30.0
CLEANUP_PASSES = 3
"""Trashing one copy can remove or merge another (as on Gmail): list again, a few passes."""
FAILED_NOTE_FOR = timedelta(hours=6)
"""How long a failed check is still named in the report's access-problem line."""

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
            health.state is not HealthState.STALE
            or not ctx.sources.hide_test_marked
            or source not in ctx.sources.daily_mail()
            or not wait.delivery_check
            or not active_health_checks_allowed()
        ):
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
            return health
        _save(ctx, source, Record(ts(started), before.pending, before.result, before.result_at))
    engine = Engine(ctx)
    if before.pending and not _clean(ctx, engine, source, grant.handle):
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
    cleaned = _clean(ctx, engine, source, grant.handle)
    keep = None if arrived and cleaned else subject
    if arrived is None:
        _save(ctx, source, Record(ts(started), keep, "missing", now))
        return health
    _save(ctx, source, Record(ts(started), keep, "arrived", ts(arrived)))
    repo.write_health(ctx.db, source, HealthState.OK, now, ts(arrived))
    return health.model_copy(update={"state": HealthState.OK, "fresh_as_of": arrived})


def _marked(
    ctx: Context, source: str, role: FolderRole, subject: str | None = None
) -> list[MessageSummary]:
    """This mailbox's delivery-check copies in the folder of `role`."""
    port = ctx.sources.mail_port(source)
    folder = next((f.id for f in port.folders(source) if f.role is role), None)
    if folder is None:
        return []
    page = port.search(source, MailQuery(folder_id=folder, text=subject or DELIVERY_MARK))
    return [
        m
        for m in page.items
        if carries_delivery_mark(m.subject)
        and f" {source} " in m.subject
        and (subject is None or m.subject.strip() == subject)
    ]


def _await(ctx: Context, source: str, subject: str, deadline: datetime) -> datetime | None:
    """The time the message was first seen in the Inbox, or None when it did not come in time."""
    port = ctx.sources.mail_port(source)
    inbox = next((f.id for f in port.folders(source) if f.role is FolderRole.INBOX), None)
    polls = int(max(0.0, (deadline - ctx.clock.now()).total_seconds()) / POLL_SECONDS) + 2
    for _ in range(polls):
        left = deadline - ctx.clock.now()
        if inbox is not None and isinstance(port, FolderSync) and left > timedelta(0):
            try:
                port.sync_folders(source, [inbox], min(left, timedelta(seconds=SYNC_SECONDS)))
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


def _clean(ctx: Context, engine: Engine, source: str, grant: str) -> bool:
    """Move every copy of this mailbox's delivery-check mail (Inbox, Sent) to Trash.

    One `mail.trash` write per folder per pass; True when none is left. A failure is logged and
    left for the next probe (the record still names the message)."""
    roles = (FolderRole.INBOX, FolderRole.SENT)
    try:
        for _ in range(CLEANUP_PASSES):
            found = [m for role in roles for m in _marked(ctx, source, role)]
            if not found:
                return True
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
                progress = progress or (done is not None and done.state is OperationState.CONFIRMED)
            if not progress:
                return False
        return not any(_marked(ctx, source, role) for role in roles)
    except Exception:
        log_failure(log, "moving the delivery-check message of %s to Trash failed", source)
        return False
