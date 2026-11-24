"""The tag checker: tells the person (through `update.available`) that a newer tag exists.

Contract: docs/contracts/07-attention.md. It only reads tag names, never pulls or changes
local work, and never runs inside a test against the network: the lister is injected.
Only tags named `<prefix>MAJOR.MINOR.PATCH[-alpha.N|-beta.N|-rc.N]` count. The result is two
system keys (`runs.update_tag`, `runs.update_checked_at`); attention raises the event from
the first.
"""

import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from chamberlain import __version__
from chamberlain.app.context import Context
from chamberlain.runs.tuning import TAG_PREFIX, UPDATE_CHECK_EVERY, UPDATE_RETRY_AFTER
from chamberlain.schema.errors import ChamberlainError
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.store.schema import get_state, set_state

log = logging.getLogger("chamberlain.runs")

TagLister = Callable[[], Sequence[str]]
"""Returns tag names. May raise `OSError`, `subprocess.SubprocessError` or `ChamberlainError`."""

_STAGES = {"alpha": 0, "beta": 1, "rc": 2}
_TAG = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$")
_INSTALLED = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?")
_SHORT = {"a": 0, "b": 1, "rc": 2}
Version = tuple[int, int, int, int, int]
FINAL = 3


def parse_tag(tag: str, prefix: str = TAG_PREFIX) -> Version | None:
    if not tag.startswith(prefix):
        return None
    match = _TAG.match(tag[len(prefix) :])
    if match is None:
        return None
    major, minor, patch, stage, number = match.groups()
    rank = FINAL if stage is None else _STAGES[stage]
    return (int(major), int(minor), int(patch), rank, int(number or 0))


def parse_installed(version: str) -> Version | None:
    match = _INSTALLED.match(version)
    if match is None:
        return None
    major, minor, patch, stage, number = match.groups()
    rank = FINAL if stage is None else _SHORT[stage]
    return (int(major), int(minor), int(patch), rank, int(number or 0))


def newest_after(tags: Sequence[str], installed: str, prefix: str = TAG_PREFIX) -> str | None:
    """The newest well-formed tag that is newer than `installed`, or None."""
    current = parse_installed(installed)
    found = [(parsed, tag) for tag in tags if (parsed := parse_tag(tag, prefix)) is not None]
    if current is None or not found:
        return None
    best = max(found)
    return best[1] if best[0] > current else None


def git_tag_lister(remote: str, *, cwd: Path | None = None, timeout: float = 8.0) -> TagLister:
    """List the tags of `remote` with `git ls-remote --tags` (read only, bounded)."""

    def lister() -> Sequence[str]:
        done = subprocess.run(
            ["git", "ls-remote", "--tags", remote],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        names: list[str] = []
        for line in done.stdout.splitlines():
            _, _, ref = line.partition("\t")
            if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
                names.append(ref[len("refs/tags/") :])
        return names

    return lister


def _due(ctx: Context, now: datetime, force: bool) -> bool:
    last = get_state(ctx.db, "runs.update_checked_at")
    if force or last is None:
        return True
    return now - datetime.fromisoformat(last.replace("Z", "+00:00")) >= UPDATE_CHECK_EVERY


def check_for_update(
    ctx: Context, lister: TagLister, *, installed: str = __version__, force: bool = False
) -> str | None:
    """Look for a newer tag at most once a day; returns the tag now recorded, or None.

    A lister that fails leaves the earlier answer in place and retries in an hour.
    """
    now = ctx.clock.now()
    if not _due(ctx, now, force):
        return recorded_update(ctx)
    try:
        tag = newest_after(lister(), installed)
    except (OSError, subprocess.SubprocessError, ChamberlainError):
        log.warning("update check failed")
        with transaction(ctx.db):
            retry = (now - UPDATE_CHECK_EVERY + UPDATE_RETRY_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
            set_state(ctx.db, ctx.clock, "runs.update_checked_at", retry)
        return recorded_update(ctx)
    with transaction(ctx.db):
        set_state(ctx.db, ctx.clock, "runs.update_tag", tag or "")
        set_state(ctx.db, ctx.clock, "runs.update_checked_at", stamp(ctx.clock))
    return tag


def recorded_update(ctx: Context) -> str | None:
    value = get_state(ctx.db, "runs.update_tag")
    return value or None


def tick_step(lister: TagLister, installed: str = __version__) -> Callable[[Context], None]:
    """A tick step that runs `check_for_update` on the daily schedule."""

    def check_updates(ctx: Context) -> None:
        check_for_update(ctx, lister, installed=installed)

    return check_updates
