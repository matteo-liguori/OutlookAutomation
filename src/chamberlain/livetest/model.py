"""What a run records: one result per scenario, one report per run.

Contract: docs/live-test.md (Reading the score). A report holds handles, codes, counts and
timings only. It never holds a subject, a body, an address or any text another person wrote.
"""

from enum import StrEnum

from pydantic import Field

from chamberlain.schema.base import Model


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


class Timing(Model):
    tool: str
    ms: int = Field(ge=0)


class ScenarioResult(Model):
    id: str
    group: str
    status: Status
    reason: str = ""
    """Why it failed or was skipped. Harness-written text, never remote text."""
    seconds: float = Field(default=0.0, ge=0)
    handles: tuple[str, ...] = ()
    """Operation, message and event handles the scenario touched."""
    timings: tuple[Timing, ...] = ()
    probe_checks: tuple[str, ...] = ()
    """Checks that used the read-only probe because the tools cannot answer them."""
    cleanup: str = "clean"
    """`clean`, `kept` (--keep) or a short description of what could not be removed."""
    manual: tuple[str, ...] = ()
    """Steps the owner does by hand, for a manual scenario."""


class Score(Model):
    passed: int
    failed: int
    skipped: int
    leftovers: int
    percent: int = Field(ge=0, le=100)
    """Passed out of passed plus failed; leftovers count as failures. 100 when nothing ran."""
    coverage_percent: int = Field(ge=0, le=100)
    """Scenarios that ran, out of all planned."""


class RunReport(Model):
    run_id: str
    started_at: str
    finished_at: str
    mode: str
    """`live`, `fake` or `dry-run`."""
    dry_run: bool = False
    kept: bool = False
    sources: tuple[str, ...] = ()
    results: tuple[ScenarioResult, ...] = ()
    score: Score
    leftovers: tuple[str, ...] = ()
    """Handles of marked items still present (not in the trash) after cleanup."""
    doctor_before: tuple[str, ...] = ()
    doctor_after: tuple[str, ...] = ()
    """`id:status` of every doctor check, before and after the run."""
    stopped: str = ""
    """Set when the run stopped before the scenarios (for example a missing grant)."""
    finished: bool = True
    """False while the run is still going: the report is rewritten after every scenario, and
    the ones not run yet say so (`NOT_RUN_YET`)."""


def compute_score(results: tuple[ScenarioResult, ...], leftovers: int) -> Score:
    passed = sum(1 for r in results if r.status is Status.PASS)
    failed = sum(1 for r in results if r.status is Status.FAIL) + leftovers
    skipped = sum(1 for r in results if r.status is Status.SKIPPED)
    ran = passed + failed
    percent = 100 if ran == 0 else round(100 * passed / ran)
    total = len(results)
    coverage = 100 if total == 0 else round(100 * (total - skipped) / total)
    return Score(
        passed=passed,
        failed=failed,
        skipped=skipped,
        leftovers=leftovers,
        percent=percent,
        coverage_percent=coverage,
    )
