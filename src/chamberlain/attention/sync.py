"""Folder sync: the mail client brings every folder the scan reads up to date; the harness waits.

Contract: docs/contracts/07-attention.md (A-24 to A-27) and 08-ports.md (P-20 to P-22). A client
that reads from its own local index (Thunderbird) can lag the server in folders it does not sync
by itself (All Mail, label and filing folders). So before the scan reads a source, before a
scheduled pass starts and when the agent starts, the harness asks for a sync of every folder the
scan reads, waits for the answer, and records when each folder was last in sync. A folder that
will not sync never blocks anything for long: the whole sync has a time limit, the scan goes on
with the folders that finished, and the folder is named in a `source.stale` event (reason `sync`)
and in the pass report until a later try gets it through.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chamberlain.app.context import Context
from chamberlain.attention import repo
from chamberlain.attention.tuning import MAIL_SKIPPED_ROLES, SYNC_FRESH, SYNC_LIMIT
from chamberlain.domain.mail import Folder
from chamberlain.ports.sync import (
    NOT_REACHED,
    NOT_SELECTABLE,
    FolderOutcome,
    FolderSync,
    SyncReport,
)
from chamberlain.runs import repo as runs_repo
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction
from chamberlain.store.schema import MAIL_SYNC_KEY, get_state

log = logging.getLogger("chamberlain.attention")

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _fmt(when: datetime) -> str:
    return when.strftime(_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _FORMAT).replace(tzinfo=UTC)


@dataclass(frozen=True)
class SyncSettings:
    fresh: timedelta
    """How old a folder's last sync may be before the scan or the agent syncs it again."""
    limit: timedelta
    """The overall time one tool call may spend syncing, all sources together. Sources are
    synced in turn and the ones the limit cuts off are tried again on the next tick."""


def settings(ctx: Context) -> SyncSettings:
    """The profile's `mail_sync`, mirrored into the store at start; defaults when absent."""
    fresh, limit = SYNC_FRESH, SYNC_LIMIT
    raw = get_state(ctx.db, MAIL_SYNC_KEY)
    if raw:
        try:
            data = json.loads(raw)
            fresh = timedelta(minutes=float(data.get("fresh_minutes", fresh.total_seconds() / 60)))
            limit = timedelta(seconds=float(data.get("time_limit_seconds", limit.total_seconds())))
        except (ValueError, TypeError, AttributeError):
            log_failure(log, "the mail_sync setting could not be read")
    return SyncSettings(fresh, limit)


def scan_folders(folders: list[Folder]) -> list[Folder]:
    """The folders the scan reads: every one except Trash, Junk and Drafts (A-20)."""
    return [f for f in folders if f.role not in MAIL_SKIPPED_ROLES]


@dataclass(frozen=True)
class Synced:
    asked: bool
    """True when the client was asked about at least one folder."""
    failed: tuple[str, ...] = ()
    """Folder ids that did not finish; the scan reads on without waiting for them."""


def _due(row: repo.SyncFolder | None, now: datetime, max_age: timedelta) -> bool:
    if row is None or row.synced_at is None or row.failure:
        return True
    return now - _parse(row.synced_at) > max_age


def _fresh_enough(ctx: Context, source: str, max_age: timedelta) -> bool:
    """A cheap look at the record: true when nothing can be due, so the port is not asked."""
    state = repo.sync_state(ctx.db, source)
    if state is None or state[0] is None:
        return False
    if ctx.clock.now() - _parse(state[0]) > max_age:
        return False
    return not any(r.failure for r in repo.sync_folder_rows(ctx.db, source))


def _time_left(ctx: Context, limit: timedelta) -> timedelta:
    """What is left of the call's overall sync limit. The clock starts at the first sync."""
    budget = ctx.sync_budget
    if budget is None:
        return limit
    now = ctx.clock.now()
    if budget.started is None:
        budget.started = now
    return limit - (now - budget.started)


def sync_source(ctx: Context, source: str, *, max_age: timedelta | None = None) -> Synced | None:
    """Sync the folders of `source` that were last in sync more than `max_age` ago (the profile's
    freshness by default), plus any that failed last time, and wait for the answer.

    None when the connector has nothing to sync (it reads the server directly). Never raises for a
    folder that would not sync: it is recorded as failed and named by `source.stale`.
    """
    config = settings(ctx)
    age = config.fresh if max_age is None else max_age
    port = ctx.sources.mail_port(source)
    if not isinstance(port, FolderSync):
        return None
    if _fresh_enough(ctx, source, age):
        return Synced(False)
    folders = scan_folders(port.folders(source))
    known = {r.folder_id: r for r in repo.sync_folder_rows(ctx.db, source)}
    asked = [f for f in folders if _due(known.get(f.id), ctx.clock.now(), age)]
    report = None
    if asked:
        # A folder already failing is asked once per run; a new one is asked again until the
        # limit, because the client usually finishes a slow folder while the second ask waits.
        retry = frozenset(f.id for f in asked if f.id not in known or not known[f.id].failure)
        left = _time_left(ctx, config.limit)
        if left <= timedelta(0):
            # The call's overall limit is used up: this source is not synced now, is reported
            # like a folder that would not sync, and is tried again on the next tick (A-25).
            report = SyncReport(
                folders=tuple(
                    FolderOutcome(
                        folder_id=f.id, synced=False, code=NOT_REACHED, at=ctx.clock.now()
                    )
                    for f in asked
                )
            )
        else:
            report = port.sync_folders(
                source, [f.id for f in asked], min(config.limit, left), retry
            )
    answers = {} if report is None else {o.folder_id: o for o in report.folders}
    failed: list[str] = []
    now = ctx.clock.now()
    with transaction(ctx.db):
        for folder in folders:
            row = known.get(folder.id)
            outcome = answers.get(folder.id)
            updated = _after(folder, row, outcome, _fmt(now))
            if updated.failure:
                failed.append(folder.id)
            repo.save_sync_folder(ctx.db, source, updated)
        repo.drop_sync_folders(ctx.db, source, [f.id for f in folders])
        rows = repo.sync_folder_rows(ctx.db, source)
        previous = repo.sync_state(ctx.db, source)
        complete = previous[0] if previous else None
        if rows and all(r.synced_at and not r.failure for r in rows):
            complete = min(r.synced_at for r in rows if r.synced_at)
        repo.save_sync_state(ctx.db, source, complete, _fmt(now))
    return Synced(bool(asked), tuple(failed))


def _after(
    folder: Folder, row: repo.SyncFolder | None, outcome: FolderOutcome | None, now: str
) -> repo.SyncFolder:
    """The folder's record once `outcome` (None: not asked this time) is known."""
    synced_at = None if row is None else row.synced_at
    failure = "" if row is None else row.failure
    catchup = None if row is None else row.catchup_from
    attempted = now if row is None else row.attempted_at
    if outcome is not None:
        attempted = _fmt(outcome.at)
        if outcome.synced or outcome.code == NOT_SELECTABLE:
            synced_at, failure = _fmt(outcome.at), ""
        else:
            failure = outcome.code or "failed"
            if catchup is None:
                # From its last good sync; a folder that never synced from this first try.
                catchup = synced_at or _fmt(outcome.at)
    return repo.SyncFolder(
        folder.id, folder.name, folder.role.value, synced_at, attempted, failure, catchup
    )


def ensure_fresh(ctx: Context, source: str) -> Synced | None:
    """Sync `source` if it is stale, for a caller that must not fail (A-7): errors are logged."""
    try:
        return sync_source(ctx, source)
    except Exception:
        log_failure(log, "folder sync failed for %s", source)
        return None


def ensure_all_fresh(ctx: Context) -> None:
    """The freshness guard when an agent starts (A-24)."""
    for source in sorted(ctx.sources.mail):
        ensure_fresh(ctx, source)


def _lead(ctx: Context, now: datetime) -> timedelta | None:
    """How far ahead of its time a scheduled pass is, when one is due within its lead."""
    longest: timedelta | None = None
    for item in runs_repo.presync_schedules(ctx.db):
        try:
            zone = ZoneInfo(item.zone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
        local = now.astimezone(zone)
        if local.isoweekday() not in item.weekdays:
            continue
        hour, minute = (int(part) for part in item.at_local.split(":"))
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        lead = timedelta(minutes=item.minutes)
        if due - lead <= local < due and (longest is None or lead > longest):
            longest = lead
    return longest


def presync(ctx: Context) -> None:
    """A few minutes before a scheduled pass, sync so the agent's pass finds fresh folders (A-26).

    A folder synced within the lead is left alone, so a window with several ticks syncs once, and
    a folder that failed is asked again on every tick until the pass is due.
    """
    lead = _lead(ctx, ctx.clock.now())
    if lead is None:
        return
    for source in sorted(ctx.sources.mail):
        try:
            sync_source(ctx, source, max_age=lead)
        except Exception:
            log_failure(log, "folder sync before a scheduled pass failed for %s", source)
