"""Tool handlers of the attention track: start, next, guide.

Contract: docs/contracts/07-attention.md and 02-tools.md. `app/wiring.py` reads the names below;
this track fills them and never edits the app package.
"""

import base64
import binascii
import logging
from typing import Literal

from chamberlain.app.context import Context, DoctorCheckFn, Handler, Hooks, Result
from chamberlain.attention import inbox, older, repo, sessions, sync
from chamberlain.attention.collect import source_health
from chamberlain.attention.hooks import HOOKS as _HOOKS
from chamberlain.attention.overrides import apply_overrides
from chamberlain.attention.rules import Rule, load_rules, rule_problems
from chamberlain.attention.tuning import GUIDE_PAGE
from chamberlain.attention.work import work_item
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.timefmt import parse
from chamberlain.limits import START_WORK_ITEMS, SUMMARY_MAX
from chamberlain.runs import repo as runs_repo
from chamberlain.runs import report_repo
from chamberlain.schema.base import ToolName
from chamberlain.schema.envelope import Coverage
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.safelog import log_failure
from chamberlain.schema.tools import (
    OWNER_ASKS_MAX,
    CheckResult,
    DoctorCheck,
    GuideRequest,
    GuideResponse,
    GuideRule,
    NextRequest,
    NextResponse,
    OwnerAsk,
    SourceStatus,
    StartRequest,
    StartResponse,
    WorkItem,
)
from chamberlain.store.migrate import stamp

SourceKind = Literal["mail", "calendar", "contacts"]

log = logging.getLogger("chamberlain.attention")

_SETUP_GUIDE = GuideRule(
    id="setup.agent",
    version=1,
    title="Set up a new installation",
    text="Follow docs/setup-agent.md. Choose the providers with the user and resume from setup "
    "status. Leave credential entry and consent to the user.",
)


def _offer(ctx: Context, session: str, limit: int) -> tuple[tuple[WorkItem, ...], int]:
    """The next events this session has not seen; marks them delivered (A-9)."""
    events = repo.candidates(ctx.db, session, limit)
    inbox.deliver(ctx.db, ctx.clock, session, events)
    return tuple(work_item(e) for e in events), repo.count_candidates(ctx.db, session)


def _capabilities(ctx: Context, source: str) -> tuple[SourceKind, list[str]]:
    role = source.split(":", 1)[0]
    kind: SourceKind = (
        "mail" if role == "mail" else "calendar" if role == "calendar" else "contacts"
    )
    try:
        return kind, ctx.sources.port(source).capabilities(source).names()
    except Exception:
        return kind, []


def _sources(ctx: Context) -> tuple[SourceStatus, ...]:
    found: list[SourceStatus] = []
    for source in ctx.sources.daily_source_ids():
        health = source_health(ctx, source)
        state = HealthState.UNAVAILABLE if health is None else health.state
        kind, names = _capabilities(ctx, source)
        found.append(SourceStatus(id=source, kind=kind, state=state, capabilities=tuple(names)))
    return tuple(found)


def start(ctx: Context, request: StartRequest) -> Result:
    # The agent is about to read mail: folders last synced too long ago are synced first (A-24).
    sync.ensure_all_fresh(ctx)
    row = sessions.start(
        ctx.db,
        ctx.clock,
        agent=request.agent or "",
        purpose=request.purpose,
        resume=request.resume,
        zone=sessions.configured_zone(ctx.data_root),
    )
    housekeeping = _on_start(ctx, request.agent or "")
    work, more = _offer(ctx, row.handle, START_WORK_ITEMS)
    asks = report_repo.waiting_for_owner(ctx.db)
    live = None if request.purpose == "scheduled" else runs_repo.live_run(ctx.db)
    response = StartResponse(
        session=row.handle,
        now=ctx.clock.now(),
        sources=_sources(ctx),
        work=work,
        more_work=more,
        housekeeping=housekeeping,
        waiting_for_you=tuple(
            OwnerAsk(action=a.action, card=a.card, title=a.title, since=parse(a.since))
            for a in asks[:OWNER_ASKS_MAX]
        )
        or None,
        waiting_more=max(0, len(asks) - OWNER_ASKS_MAX),
        live_report=None if live is None else live.handle,
    )
    return Result(response, next=(work[0].next,) if work else ())


def _on_start(ctx: Context, agent: str) -> tuple[str, ...]:
    """Other tracks' start steps (the owner's mail clean-up); a failure never fails `start`."""
    lines: list[str] = []
    for step in ctx.tick_steps.on_start:
        try:
            lines += step(ctx, agent)
        except Exception:
            log_failure(log, "start step %s failed", getattr(step, "__name__", "step"))
    return tuple(line[:SUMMARY_MAX] for line in lines[:10])


def next_work(ctx: Context, request: NextRequest) -> Result:
    session = ctx.session
    if session is None:
        raise ChamberlainError(ErrorCode.SESSION_REQUIRED, "This tool needs a session.")
    row = repo.get_session(ctx.db, session)
    if row is not None and sessions.is_stale_scheduled(
        row, ctx.clock, sessions.configured_zone(ctx.data_root)
    ):
        raise sessions.stop_error()
    if request.handled or request.older_mail:
        older.next_with_answers(ctx, request.handled, request.older_mail, actor=f"agent:{session}")
    work, more = _offer(ctx, session, request.limit)
    return Result(NextResponse(work=work, remaining=more))


def _encode(offset: int) -> str:
    return base64.urlsafe_b64encode(f"g{offset}".encode()).decode()


def _decode(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        if raw.startswith("g") and raw[1:].isdigit():
            return int(raw[1:])
    except (binascii.Error, UnicodeDecodeError):
        pass
    raise ChamberlainError(
        ErrorCode.REQUEST_BAD_CURSOR,
        "That page cursor is not valid.",
        fix="Call guide again without a cursor.",
    )


def effective_rules(ctx: Context) -> tuple[Rule, ...]:
    """The shared rules with the owner's private patches applied (A-18)."""
    return apply_overrides(load_rules(), ctx.data_root).rules


def guide(ctx: Context, request: GuideRequest) -> Result:
    rules = (*effective_rules(ctx), _SETUP_GUIDE)
    if request.id is not None:
        found = next((r for r in rules if r.id == request.id), None)
        if found is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_UNKNOWN_HANDLE,
                "There is no such guidance rule.",
                fix="Call guide without an id to list the rules.",
            )
        if ctx.session is not None and repo.get_session(ctx.db, ctx.session) is not None:
            repo.record_delivery(
                ctx.db, ctx.session, found.id, found.version, "session", stamp(ctx.clock)
            )
        item = GuideRule(id=found.id, version=found.version, title=found.title, text=found.text)
        return Result(GuideResponse(rules=(item,)))
    start_at = _decode(request.cursor)
    page = rules[start_at : start_at + GUIDE_PAGE]
    more = start_at + GUIDE_PAGE < len(rules)
    items = tuple(GuideRule(id=r.id, version=r.version, title=r.title) for r in page)
    coverage = Coverage(
        complete=not more,
        returned=len(items),
        total=len(rules),
        cursor=_encode(start_at + GUIDE_PAGE) if more else None,
    )
    return Result(GuideResponse(rules=items), coverage=coverage)


def guidance_checks(ctx: Context) -> list[CheckResult]:
    """Sizes, content, triggers and stale overrides (A-12, A-13, A-18)."""
    shared = load_rules()
    problems = rule_problems(shared)
    total = sum(len(r.text) for r in shared)
    applied = apply_overrides(shared, ctx.data_root)
    flagged = bool(applied.stale or applied.problems)
    summary = problems[0] if problems else f"{len(shared)} rules, {total} characters."
    return [
        CheckResult(
            id="guidance.rules",
            status="fail" if problems else "ok",
            summary=summary[:160],
            fix="Report this: the shared guidance is invalid." if problems else None,
        ),
        CheckResult(
            id="guidance.overrides",
            status="warn" if flagged else "ok",
            summary=(
                f"{len(applied.stale)} overrides need re-checking, "
                f"{len(applied.problems)} were ignored."
            ),
            fix="Review the private guidance patches against the current rules."
            if flagged
            else None,
        ),
    ]


def source_checks(ctx: Context) -> list[CheckResult]:
    health = repo.read_health(ctx.db)
    bad = sorted(s for s, state in health.items() if state is not HealthState.OK)
    summary = f"{len(bad)} of {len(health)} sources need attention."
    held = repo.mail_held(ctx.db)
    unsynced = repo.sync_failures(ctx.db)
    return [
        CheckResult(
            id="sources.health",
            status="warn" if bad else "ok",
            summary=(summary + (" " + ", ".join(bad) if bad else ""))[:160],
            fix="Ask the user to check the listed sources." if bad else None,
        ),
        CheckResult(
            id="sources.older_mail",
            status="warn" if held else "ok",
            summary=(
                "; ".join(f"{s}: {h.start} to {h.end} UTC, {h.state}" for s, h in held)
                if held
                else "No mail is held back as too old."
            )[:160],
            fix="Ask the owner whether to read it, then answer through next." if held else None,
        ),
        CheckResult(
            id="sources.sync",
            status="warn" if unsynced else "ok",
            summary=_sync_summary(ctx, unsynced),
            fix="Say the report is partly verified; the folders are tried again every tick."
            if unsynced
            else None,
        ),
    ]


def _sync_summary(ctx: Context, unsynced: list[tuple[str, repo.SyncFolder]]) -> str:
    if unsynced:
        counts: dict[str, int] = {}
        for source, _ in unsynced:
            counts[source] = counts.get(source, 0) + 1
        return "; ".join(f"{s}: {n} folder(s) not synced" for s, n in counts.items())[:160]
    done = [(s, repo.sync_state(ctx.db, s)) for s in ctx.sources.daily_mail()]
    last = [f"{s}: {st[0]}" for s, st in done if st is not None and st[0] is not None]
    if not last:
        return "No folder sync has been recorded."
    return ("Every folder was synced. Last complete: " + ", ".join(last))[:160]


HANDLERS: dict[ToolName, Handler] = {
    ToolName.START: start,
    ToolName.NEXT: next_work,
    ToolName.GUIDE: guide,
}
"""Tool name to handler, for the tools this track owns (schema/registry.py)."""

DOCTOR_CHECKS: list[tuple[DoctorCheck, DoctorCheckFn]] = [
    (DoctorCheck.GUIDANCE, guidance_checks),
    (DoctorCheck.SOURCES, source_checks),
]
"""Checks this track contributes to `doctor`."""

HOOKS: Hooks | None = _HOOKS
"""The tick and reply decorator (A-6, A-19)."""
