"""The `doctor` tool: collects checks from every track.

Contract: docs/contracts/02-tools.md (doctor) and docs/contracts/10-targets.md.
The app owns the handler; each track adds its checks to `DOCTOR_CHECKS` in its
own `handlers.py`; the sixteen `--metrics` figures are collected by `app/metrics.py`.
"""

from collections.abc import Sequence

from chamberlain.app.context import Context, DoctorCheckFn, Handler, Result
from chamberlain.app.metrics import collect_metrics
from chamberlain.memory.history import stored_bytes_history
from chamberlain.schema.tools import CheckResult, DoctorCheck, DoctorRequest, DoctorResponse
from chamberlain.store.db import table_names
from chamberlain.store.migrate import discover, pending
from chamberlain.store.schema import TABLE_CLASSES


def schema_checks(ctx: Context) -> list[CheckResult]:
    """Migrations are applied and every table has a data class (S-6, S-9)."""
    waiting = pending(ctx.db, discover())
    unclassified = sorted(table_names(ctx.db) - set(TABLE_CLASSES))
    return [
        CheckResult(
            id="schema.migrations",
            status="ok" if not waiting else "fail",
            summary=f"{len(waiting)} migrations waiting to be applied.",
            fix=None if not waiting else "Restart Chamberlain so the store is upgraded.",
        ),
        CheckResult(
            id="schema.table_classes",
            status="ok" if not unclassified else "fail",
            summary=f"{len(unclassified)} tables have no lifetime class.",
            fix=None if not unclassified else "Report this: a table has no lifetime rule.",
        ),
    ]


def make_doctor(extra: Sequence[tuple[DoctorCheck, DoctorCheckFn]] = ()) -> Handler:
    """Build the doctor handler from the app's checks plus the tracks' checks."""
    checks: list[tuple[DoctorCheck, DoctorCheckFn]] = [(DoctorCheck.SCHEMA, schema_checks), *extra]

    def doctor(ctx: Context, request: DoctorRequest) -> Result:
        results: list[CheckResult] = []
        for group, check in checks:
            if request.check in (DoctorCheck.ALL, group):
                results.extend(check(ctx))
        metrics = collect_metrics(ctx) if request.metrics else None
        history = stored_bytes_history(ctx.db) if request.metrics else None
        return Result(
            DoctorResponse(checks=tuple(results), metrics=metrics, stored_bytes_history=history)
        )

    return doctor
