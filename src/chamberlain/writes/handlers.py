"""Tool handlers of the writes track: propose, do, reconcile; and the audit check.

Contract: docs/contracts/04-writes.md, 05-grants.md, 02-tools.md.
"""

import re
from collections.abc import Callable

from chamberlain.app.context import Context, DoctorCheckFn, Handler, Result
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.operations import OperationState
from chamberlain.limits import PAGE_CHARS
from chamberlain.schema.base import ToolName
from chamberlain.schema.envelope import Content, ContentKind, NextStep
from chamberlain.schema.tools import (
    CheckResult,
    DoctorCheck,
    DoRequest,
    DoResponse,
    ProposeRequest,
    ProposeResponse,
    ReconcileRequest,
    ReconcileResponse,
)
from chamberlain.writes import repo, resend
from chamberlain.writes.audit import verify
from chamberlain.writes.core import Engine
from chamberlain.writes.models import Proposal


def _propose_response(proposal: Proposal) -> ProposeResponse:
    text = proposal.preview[:PAGE_CHARS]
    return ProposeResponse(
        operation=None if proposal.operation is None else proposal.operation.handle,
        grant=None if proposal.grant is None else proposal.grant.handle,
        state=proposal.state,
        requirement=proposal.requirement,
        ready=proposal.ready,
        content_sha256=proposal.content_sha256,
        preview=Content(
            origin=proposal.origin,
            kind=ContentKind.PREVIEW,
            text=text,
            truncated=proposal.truncated or len(proposal.preview) > len(text),
        ),
    )


def make_handlers(pause: Callable[[float], None] | None = None) -> dict[ToolName, Handler]:
    """Handlers use each call's context. `pause` replaces waiting on the clock (tests)."""

    def engine(ctx: Context) -> Engine:
        return Engine(ctx, pause=pause)

    def propose(ctx: Context, request: ProposeRequest) -> Result:
        eng = engine(ctx)
        if request.intent is not None:
            proposal = eng.propose_intent(
                request.intent,
                card=request.card,
                evidence=request.evidence,
                hold_for=request.action,
            )
        elif request.redo_of is not None:
            proposal = resend.propose_redo(
                eng, request.redo_of, card=request.card, evidence=request.evidence
            )
        elif request.standing is not None:
            proposal = eng.propose_standing(request.standing, card=request.card)
        else:
            assert request.operation is not None
            assert request.approval_evidence is not None
            proposal = eng.attach_approval(request.operation, request.approval_evidence)
        steps: tuple[NextStep, ...] = ()
        if proposal.ready and proposal.operation is not None:
            steps = (
                NextStep(
                    tool=ToolName.DO,
                    args={"operation": proposal.operation.handle},
                    why="Permission is in place; execute it once.",
                ),
            )
        return Result(_propose_response(proposal), next=steps)

    def do(ctx: Context, request: DoRequest) -> Result:
        eng = engine(ctx)
        op = eng.do(request.operation, request.report)
        steps: tuple[NextStep, ...] = ()
        if op.kind is IntentKind.OUTSIDE_ACTION and op.state is OperationState.AUTHORISED:
            steps = (
                NextStep(
                    tool=ToolName.DO,
                    args={"operation": op.handle},
                    why="Act first. Then call this again, adding report: what you did.",
                ),
            )
        return Result(DoResponse(operation=eng.view(op)), next=steps)

    def reconcile(ctx: Context, request: ReconcileRequest) -> Result:
        eng = engine(ctx)
        op, changed = eng.reconcile(request.operation, happened_evidence=request.evidence)
        return Result(ReconcileResponse(operation=eng.view(op), changed=changed))

    return {
        ToolName.PROPOSE: propose,
        ToolName.DO: do,
        ToolName.RECONCILE: reconcile,
    }


def audit_checks(ctx: Context) -> list[CheckResult]:
    """The chain verifies, and the confirmation channel says what it is (G-18, G-21)."""
    report = verify(ctx.db)
    chain = CheckResult(
        id="audit.chain",
        status="ok" if report.ok else "fail",
        summary=(
            f"The audit chain of {report.rows} rows verifies."
            if report.ok
            else f"The audit chain breaks at seq {report.first_bad_seq}: {report.problem}."
        ),
        fix=None if report.ok else "Do not write more. Restore the store from a backup.",
    )
    channel = CheckResult(
        id="audit.confirmation_channel",
        status="ok",
        summary="Viewer confirmation raises the bar against a manipulated agent; not a sandbox.",
    )
    return [chain, channel, *recipient_checks(ctx)]


def check_id(prefix: str, source: str) -> str:
    """A doctor check id for one source: only the characters a check id may hold."""
    return f"{prefix}.{re.sub(r'[^a-z0-9_]', '_', source.lower())}"[:40]


def recipient_checks(ctx: Context) -> list[CheckResult]:
    """How many recipients each mail source knows, by origin (G-15)."""
    found: list[CheckResult] = []
    for source in sorted(ctx.sources.mail):
        counts = repo.recipient_counts(ctx.db, source)
        seeded = counts["sent-history"]
        found.append(
            CheckResult(
                id=check_id("recipients", source),
                status="ok" if seeded else "warn",
                summary=(
                    f"{source}: {counts['confirmed-grant']} confirmed, {seeded} from Sent "
                    f"history, {counts['own']} own address."
                ),
                fix=None
                if seeded
                else "Run `chamberlain recipients seed` with the mail client open.",
            )
        )
    return found


HANDLERS: dict[ToolName, Handler] = make_handlers()
"""Tool name to handler, for the tools this track owns (schema/registry.py)."""

DOCTOR_CHECKS: list[tuple[DoctorCheck, DoctorCheckFn]] = [(DoctorCheck.AUDIT, audit_checks)]
"""Checks this track contributes to `doctor`."""
