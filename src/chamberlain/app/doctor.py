"""The `doctor` tool: collects checks from every track.

Contract: docs/contracts/02-tools.md (doctor) and docs/contracts/10-targets.md.
The app owns the handler; each track adds its checks to `DOCTOR_CHECKS` in its
own `handlers.py`; the sixteen `--metrics` figures are collected by `app/metrics.py`.
"""

import os
from collections.abc import Sequence

from chamberlain.app.context import Context, DoctorCheckFn, Handler, Result
from chamberlain.app.metrics import collect_metrics
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.memory.history import stored_bytes_history
from chamberlain.schema.errors import ChamberlainError
from chamberlain.schema.tools import CheckResult, DoctorCheck, DoctorRequest, DoctorResponse
from chamberlain.store.db import table_names
from chamberlain.store.migrate import discover, pending
from chamberlain.store.schema import TABLE_CLASSES


def _check(check_id: str, ok: bool, summary: str, fix: str) -> CheckResult:
    return CheckResult(
        id=check_id, status="ok" if ok else "fail", summary=summary, fix=None if ok else fix
    )


def schema_checks(ctx: Context) -> list[CheckResult]:
    """Migrations are applied and every table has a data class (S-6, S-9)."""
    waiting = pending(ctx.db, discover())
    unclassified = sorted(table_names(ctx.db) - set(TABLE_CLASSES))
    return [
        _check(
            "schema.migrations",
            not waiting,
            f"{len(waiting)} migrations waiting to be applied.",
            "Restart Chamberlain so the store is upgraded.",
        ),
        _check(
            "schema.table_classes",
            not unclassified,
            f"{len(unclassified)} tables have no lifetime class.",
            "Report this: a table has no lifetime rule.",
        ),
    ]


def access_checks(ctx: Context) -> list[CheckResult]:
    """Verify where the private data lives and whether this process can list its root."""
    try:
        root = refuse_inside_checkout(ctx.data_root)
        with os.scandir(root) as entries:
            next(entries, None)
    except ChamberlainError:
        result = _check(
            "access.data_root",
            False,
            "The data folder is inside the code checkout.",
            "Choose a private data folder outside the code checkout.",
        )
    except OSError:
        result = _check(
            "access.data_root",
            False,
            "The process could not list the data folder.",
            "Check that the configured data folder exists and can be read.",
        )
    else:
        result = _check(
            "access.data_root",
            True,
            "The data folder is outside the code checkout and can be listed.",
            "",
        )
    return [result]


def make_doctor(extra: Sequence[tuple[DoctorCheck, DoctorCheckFn]] = ()) -> Handler:
    """Build the doctor handler from the app's checks plus the tracks' checks."""
    checks: list[tuple[DoctorCheck, DoctorCheckFn]] = [
        (DoctorCheck.SCHEMA, schema_checks),
        (DoctorCheck.ACCESS, access_checks),
        *extra,
    ]

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
