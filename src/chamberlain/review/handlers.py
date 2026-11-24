"""The review track's face on the app: one doctor check, no tools of its own.

Contract: docs/contracts/06-memory.md (M-40). The check looks only at the settings file the guard
recorded in the data root; it never searches the home folder.
"""

from chamberlain.app.context import Context, DoctorCheckFn
from chamberlain.review import claude_guard
from chamberlain.schema.tools import CheckResult, DoctorCheck


def access_checks(ctx: Context) -> list[CheckResult]:
    state = claude_guard.check(ctx.data_root)
    if state.present:
        return [
            CheckResult(
                id="access.claude_guard",
                status="ok",
                summary="Claude Code's file tools are denied the data root.",
            )
        ]
    if state.where == "none":
        # Never switched on: off is the default while the harness is developed (owner).
        return [
            CheckResult(
                id="access.claude_guard",
                status="ok",
                summary="The Claude file guard is off (switch on: chamberlain guard on).",
            )
        ]
    return [
        CheckResult(
            id="access.claude_guard",
            status="warn",
            summary="The Claude file guard was switched on but its rules are gone.",
            fix="Run: chamberlain guard on (or chamberlain guard off to stop this warning).",
        )
    ]


DOCTOR_CHECKS: list[tuple[DoctorCheck, DoctorCheckFn]] = [(DoctorCheck.ACCESS, access_checks)]
