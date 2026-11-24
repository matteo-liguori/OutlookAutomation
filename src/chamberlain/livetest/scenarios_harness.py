"""Harness scenarios: guidance budgets, and doctor after the run.

Contract: docs/live-test.md (Scenarios). These test the harness itself, not a connector.
"""

from chamberlain.limits import GUIDANCE_REPLY_CHARS, MAX_REPLY_CHARS
from chamberlain.livetest.lab import Lab
from chamberlain.livetest.registry import Scope, Target, scenario


def doctor_states(lab: Lab) -> dict[str, str]:
    """`{check id: status}` of a full doctor run."""
    data = lab.ok("doctor")
    return {str(check["id"]): str(check["status"]) for check in data["checks"]}


@scenario(
    "harness.guidance_budgets",
    "harness",
    "start, next and guide stay within the guidance and reply budgets",
    Scope.GLOBAL,
)
def guidance_budgets(lab: Lab, t: Target) -> None:
    replies = {
        "start": lab.call("start", agent="live-test", resume=lab.session),
        "next": lab.call("next", limit=5),
        "guide": lab.call("guide"),
    }
    for name, reply in replies.items():
        lab.check(reply.ok, f"{name} failed: {reply.code}")
        lab.check(
            reply.guidance_chars <= GUIDANCE_REPLY_CHARS,
            f"{name} carried {reply.guidance_chars} characters of guidance "
            f"(budget {GUIDANCE_REPLY_CHARS})",
        )
        lab.check(
            reply.chars <= MAX_REPLY_CHARS,
            f"{name} replied with {reply.chars} characters (cap {MAX_REPLY_CHARS})",
        )


@scenario(
    "harness.doctor_clean",
    "harness",
    "doctor shows nothing new after the run",
    Scope.GLOBAL,
    last=True,
)
def doctor_clean(lab: Lab, t: Target) -> None:
    now = doctor_states(lab)
    failing = sorted(name for name, status in now.items() if status == "fail")
    lab.check(not failing, f"doctor fails: {', '.join(failing)}")
    worse = sorted(
        name
        for name, status in now.items()
        if status == "warn" and lab.baseline.get(name) != "warn"
    )
    lab.check(not worse, f"doctor has new warnings: {', '.join(worse)}")
    stuck = [
        handle
        for handle in sorted(lab.run_handles)
        if handle.startswith("op_")
        and handle not in lab.accepted_uncertain
        and str(lab.ok("open", handle=handle)["view"]["state"]) in {"uncertain", "reconciling"}
    ]
    lab.check(not stuck, f"{len(stuck)} operation(s) of this run are still unsettled")
