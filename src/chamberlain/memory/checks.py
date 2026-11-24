"""Storage checks for `doctor`.

Contract: docs/contracts/02-tools.md (doctor) and 06-memory.md (M-23, M-26, M-27).
"""

from typing import Literal

from chamberlain.app.context import Context
from chamberlain.limits import EVIDENCE_PER_OPEN_CARD_TARGET
from chamberlain.memory import repo
from chamberlain.memory.artifacts import blob_path
from chamberlain.memory.timeutil import minus
from chamberlain.schema.tools import CheckResult
from chamberlain.store.schema import get_state

_SWEEP_STALE_HOURS = 24


def _status(ok: bool) -> Literal["ok", "warn"]:
    return "ok" if ok else "warn"


def storage_checks(ctx: Context) -> list[CheckResult]:
    results: list[CheckResult] = []
    last = get_state(ctx.db, "memory.last_sweep_at")
    stale = last is None or last < minus(ctx.clock.now(), hours=_SWEEP_STALE_HOURS)
    results.append(
        CheckResult(
            id="storage.sweep",
            status=_status(not stale),
            summary="The sweeper has run recently."
            if not stale
            else "The sweeper has not run today.",
            fix=None if not stale else "Use any tool to trigger a tick; run doctor again.",
        )
    )
    pending = int(get_state(ctx.db, "memory.pending_deletions") or "0")
    results.append(
        CheckResult(
            id="storage.deletions",
            status=_status(pending == 0),
            summary=f"{pending} files could not be deleted and will be retried.",
            fix=None if pending == 0 else "Close programs that may hold files open; it retries.",
        )
    )
    held = len(repo.held_quarantine(ctx.db))
    results.append(
        CheckResult(
            id="storage.orphans",
            status=_status(held == 0),
            summary=f"{held} unknown files are held in quarantine.",
            fix=None if held == 0 else "Tell the user; they are deleted after 14 days.",
        )
    )
    over = get_state(ctx.db, "memory.over_budget") or "0"
    results.append(
        CheckResult(
            id="storage.budget",
            status="ok" if over == "0" else "fail",
            summary="Storage is within its budget." if over == "0" else "Storage is over budget.",
            fix=None if over == "0" else "Forget the largest stored items you no longer need.",
        )
    )
    missing = sum(
        1
        for row in repo.live_artifacts(ctx.db)
        if not blob_path(ctx.data_root, str(row["sha256"])).exists()
    )
    results.append(
        CheckResult(
            id="storage.files",
            status="ok" if missing == 0 else "fail",
            summary=f"{missing} stored items have lost their file.",
            fix=None if missing == 0 else "Fetch those items again; the sweeper clears the rows.",
        )
    )
    crowded = repo.scalar(
        ctx.db,
        "SELECT count(*) FROM (SELECT c.handle FROM cards c WHERE c.state = 'open' AND "
        "(SELECT count(DISTINCT e.evidence) FROM evidence_refs e WHERE e.owner = c.handle "
        "OR e.owner IN (SELECT handle FROM actions WHERE card = c.handle)) > ?)",
        (EVIDENCE_PER_OPEN_CARD_TARGET,),
    )
    results.append(
        CheckResult(
            id="storage.evidence",
            status=_status(crowded == 0),
            summary=f"{crowded} open cards hold over {EVIDENCE_PER_OPEN_CARD_TARGET} evidence rows.",
            fix=None if crowded == 0 else "Forget evidence a card no longer needs.",
        )
    )
    flagged = repo.scalar(ctx.db, "SELECT count(*) FROM cards WHERE over_cap = 1")
    results.append(
        CheckResult(
            id="storage.cards_over_cap",
            status=_status(flagged == 0),
            summary=f"{flagged} imported cards are over their size cap.",
            fix=None if flagged == 0 else "Update each flagged card so it fits.",
        )
    )
    return results
