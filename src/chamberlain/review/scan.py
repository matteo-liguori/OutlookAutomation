"""The improvisation log: read the agent's transcripts and count what it ran outside Chamberlain.

Contract: docs/contracts/06-memory.md (M-40). A session counts when it ran in this installation's
checkout or called a Chamberlain tool. Each transcript file is read once, from a stored byte
position, so a rescan never counts a line twice. Nothing but tool names and short summaries is kept.
"""

import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from chamberlain.app.context import Context
from chamberlain.review import repo, rounds, transcripts
from chamberlain.review.homes import AgentHomes
from chamberlain.review.transcripts import CLAUDE, CODEX
from chamberlain.review.tuning import (
    BYTES_PER_SCAN,
    FILES_PER_SCAN,
    HEAD_BYTES,
    LOOKBACK,
    MAX_LINE_BYTES,
    SCAN_EVERY,
)
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.store.schema import get_state, set_state

log = logging.getLogger("chamberlain.review")

Counts = dict[tuple[str, str, str], list[str | int]]
"""`(agent, tool, pattern)` to `[count, first_seen, last_seen]`."""


@dataclass(frozen=True)
class ReviewSettings:
    """What the tick step needs; built once by the configuration step."""

    homes: AgentHomes
    checkout: Path | None
    every_days: int = 21


def checkout_root() -> Path | None:
    """The folder this installation's code runs from, when it is a source checkout."""
    here = Path(__file__).resolve()
    for folder in here.parents:
        if (folder / ".git").exists() and (folder / "pyproject.toml").exists():
            return folder
    return None


def _same_or_inside(cwd: str, root: Path | None) -> bool:
    if root is None:
        return False
    child = os.path.normcase(os.path.realpath(cwd))
    parent = os.path.normcase(os.path.realpath(root))
    return child == parent or child.startswith(parent + os.sep)


def _files(homes: AgentHomes) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path, float]] = []
    for agent, folder in ((CLAUDE, homes.claude_transcripts), (CODEX, homes.codex_transcripts)):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.jsonl"):
            try:
                found.append((agent, path, path.stat().st_mtime))
            except OSError:
                continue
    found.sort(key=lambda item: item[2], reverse=True)
    return [(agent, path) for agent, path, _ in found[:FILES_PER_SCAN]]


def _key(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()


def _bytes(path: Path, start: int, end: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(end - start)


def _add(counts: Counts, calls: list[transcripts.Call], now: str) -> None:
    for call in calls:
        when = min(call.when or now, now)
        for pattern in call.patterns or ("",):
            entry = counts.setdefault((call.agent, call.tool, pattern), [0, when, when])
            entry[0] = int(entry[0]) + 1
            entry[1] = min(str(entry[1]), when)
            entry[2] = max(str(entry[2]), when)


def _scan_file(
    agent: str,
    path: Path,
    prior: repo.FileRow | None,
    budget: int,
    settings: ReviewSettings,
    counts: Counts,
    now: str,
) -> tuple[repo.FileRow, int]:
    """Read what is new in one file. Returns its new row and the bytes read."""
    size = path.stat().st_size
    pos = prior.byte_pos if prior else 0
    qualified = prior.qualified if prior else False
    if prior and size < pos:
        pos, qualified = 0, False
    if size == pos:
        return repo.FileRow(pos, size, qualified), 0
    end = min(size, pos + budget)
    region = _bytes(path, pos, end)
    spent = len(region)
    if qualified:
        read = transcripts.read_region(agent, region)
        if read.consumed == 0 and spent >= MAX_LINE_BYTES:
            read.consumed = spent
        _add(counts, read.calls, now)
        return repo.FileRow(pos + read.consumed, size, True), spent
    head = transcripts.read_region(agent, _bytes(path, 0, min(size, HEAD_BYTES)), want_calls=False)
    if head.cwd and _same_or_inside(head.cwd, settings.checkout):
        read = transcripts.read_region(agent, region)
        _add(counts, read.calls, now)
        return repo.FileRow(pos + read.consumed, size, True), spent
    if b"chamberlain" in region.lower():
        whole = _bytes(path, 0, end)
        spent += len(whole)
        read = transcripts.read_region(agent, whole)
        if read.chamberlain:
            _add(counts, read.calls, now)
            return repo.FileRow(read.consumed, size, True), spent
        return repo.FileRow(read.consumed, size, False), spent
    last = region.rfind(b"\n")
    return repo.FileRow(pos + (last + 1 if last >= 0 else 0), size, False), spent


def scan(ctx: Context, settings: ReviewSettings) -> int:
    """Read every transcript with something new. Returns how many calls were counted."""
    now_dt = ctx.clock.now()
    now = stamp(ctx.clock)
    counts: Counts = {}
    rows: list[tuple[str, repo.FileRow]] = []
    budget = BYTES_PER_SCAN
    for agent, path in _files(settings.homes):
        if budget <= 0:
            break
        key = _key(path)
        try:
            prior = repo.get_file(ctx.db, key)
            if prior is None and _too_old(path, now_dt):
                continue
            row, spent = _scan_file(agent, path, prior, budget, settings, counts, now)
        except OSError:
            log_failure(log, "a transcript could not be read")
            continue
        budget -= spent
        if prior is None or row != prior:
            rows.append((key, row))
    with transaction(ctx.db):
        for key, row in rows:
            repo.put_file(ctx.db, key, row.byte_pos, row.size, row.qualified, now)
        repo.add_counts(
            ctx.db,
            [(a, t, p, int(v[0]), str(v[1]), str(v[2])) for (a, t, p), v in counts.items()],
            now,
        )
        set_state(ctx.db, ctx.clock, "review.last_scan_at", now)
    return sum(int(v[0]) for v in counts.values())


def _too_old(path: Path, now: datetime) -> bool:
    return now.timestamp() - path.stat().st_mtime > LOOKBACK.total_seconds()


def scan_due(ctx: Context) -> bool:
    """At most once a day, and once more after a scheduled pass ends."""
    last = get_state(ctx.db, "review.last_scan_at")
    if last is None:
        return True
    if ctx.clock.now() - datetime.fromisoformat(last.replace("Z", "+00:00")) >= SCAN_EVERY:
        return True
    return repo.pass_finished_since(ctx.db, last)


def tick_step(settings: ReviewSettings) -> Callable[[Context], None]:
    """The tick step: scan when due, then open or close review rounds. Never raises."""

    def review_step(ctx: Context) -> None:
        try:
            if scan_due(ctx):
                scan(ctx, settings)
        except Exception:
            log_failure(log, "the transcript scan failed")
        try:
            rounds.refresh(ctx, settings.every_days)
        except Exception:
            log_failure(log, "the review round check failed")

    return review_step
