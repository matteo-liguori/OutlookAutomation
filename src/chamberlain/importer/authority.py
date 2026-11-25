"""Grants and write journals from the old harness, written through writes' repository.

Contract: docs/contracts/04-writes.md, 05-grants.md (G-6) and the rebuild plan.

Safety rules, in the order they matter:

1. **A journal not in a final state is closed as `abandoned`**: it was left with the old
   harness, which owns its own journals. It is counted in the report, never shown to the agent
   as work and never resent. The allowlist of final stages is closed: any stage this code has
   not seen is treated the same way. An imported operation has no stored intent and no
   prepared write, so nothing can dispatch it.
2. **An old approval authorises nothing new.** The old message hash cannot equal a new
   canonical intent hash, so an imported one-off grant is inert by construction. It is
   imported with its original dates and hash for the record, not re-hashed.
3. **Standing grants are re-confirmed.** Each imports as `suspended` with human
   confirmation required, no scope (the old scope was free text, not a `StandingScope`)
   and a review due now. The old wording is kept as evidence so the owner can recreate
   it. No recipient becomes known (G-15).
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, cast
from zoneinfo import ZoneInfo

from chamberlain.app.services import AuditLog
from chamberlain.domain.grants import GrantKind, GrantState, Requirement
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.operations import OperationState, Receipt, Verification
from chamberlain.importer.convert import add_days, derive, is_sha256, norm_ts, sha256_text, shorten
from chamberlain.importer.old import Json, OldApproval, OldInstallation, OldJournal
from chamberlain.importer.profile_map import MappedProfile
from chamberlain.importer.records import Imported, add_evidence
from chamberlain.importer.report import ImportReport
from chamberlain.importer.summary import attempted_at, summarise
from chamberlain.limits import EVIDENCE_MAX, EVIDENCE_PER_OPEN_CARD_TARGET
from chamberlain.memory import repo as memory
from chamberlain.memory.timeutil import parse
from chamberlain.writes import repo as writes
from chamberlain.writes.imported import OperationSummary
from chamberlain.writes.models import StoredReceipt

ACTOR = "system:import"
ONE_OFF_DAYS = 7
STANDING_DAYS = 365
DONE_STATUSES = frozenset({"sent", "verified-sent", "done", "completed"})
DEAD_STATUSES = frozenset(
    {"revoked", "withdrawn", "replaced", "superseded", "dismissed", "cancelled"}
)
SUCCESS_STAGES = frozenset({"complete", "submitted", "abandoned"})
"""Journal stages that record a finished write (`abandoned` is a completed draft discard)."""
CLEAN_STAGES = frozenset({"not-attempted"})
"""A journal that records that nothing was sent."""

_KINDS: dict[str, IntentKind] = {
    "send": IntentKind.MAIL_SEND,
    "draft-save": IntentKind.MAIL_DRAFT_SAVE,
    "draft-update": IntentKind.MAIL_DRAFT_UPDATE,
    "update": IntentKind.MAIL_DRAFT_UPDATE,
    "draft-discard": IntentKind.MAIL_DRAFT_DISCARD,
    "move": IntentKind.MAIL_MOVE,
    "trash": IntentKind.MAIL_TRASH,
    "mark-read": IntentKind.MAIL_MARK_READ,
    "calendar-create": IntentKind.CALENDAR_CREATE,
    "calendar-update": IntentKind.CALENDAR_UPDATE,
    "calendar-delete": IntentKind.CALENDAR_DELETE,
}


@dataclass
class Context:
    db: sqlite3.Connection
    audit: AuditLog
    old: OldInstallation
    mapped: MappedProfile
    zone: ZoneInfo
    now: str
    report: ImportReport
    imported: Imported


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def parse_key(key: str) -> tuple[str, str]:
    """`(operation name, approval id)` from a journal key.

    `thunderbird:send:<id>`, `native-send:<id>` and `outlook-send:<id>` all name a send.
    """
    head, _, rest = key.partition(":")
    if head == "thunderbird":
        operation, _, approval = rest.partition(":")
        return operation, approval
    return head.partition("-")[2], rest


def _source_for(ctx: Context, *candidates: Any, calendar: bool = False) -> str:
    for candidate in candidates:
        text = _text(candidate)
        if not calendar and text.lower() in ctx.mapped.mail_by_address:
            return ctx.mapped.mail_by_address[text.lower()]
        if calendar and text in ctx.mapped.calendar_by_id:
            return ctx.mapped.calendar_by_id[text]
    if calendar:
        return ctx.mapped.default_calendar or "calendar:unknown"
    return ctx.mapped.default_mail or "mail:unknown"


def _journal_outcomes(old: OldInstallation) -> dict[str, str]:
    """Approval id to `success` when a journal records its write as finished."""
    done: dict[str, str] = {}
    for journal in old.journals:
        _, approval = parse_key(journal.key)
        if approval and journal.state.get("stage") in SUCCESS_STAGES:
            done[approval] = "success"
    return done


def _grant_title(approval: OldApproval) -> str:
    message = _mapping(approval.details.get("message"))
    return shorten(
        _text(approval.details.get("title")) or _text(message.get("subject")) or approval.id, 80
    )


def _grant_source(ctx: Context, approval: OldApproval) -> str:
    message = _mapping(approval.details.get("message"))
    scope = _mapping(approval.details.get("scope"))
    if _text(scope.get("operation")).startswith("calendar"):
        return _source_for(ctx, scope.get("calendar_id"), calendar=True)
    return _source_for(ctx, message.get("sender"), scope.get("mailbox"), scope.get("store"))


def _scope_statement(approval: OldApproval) -> str:
    parts = {
        "scope": approval.details.get("scope"),
        "triggers": approval.details.get("triggers"),
        "boundaries": approval.details.get("boundaries"),
    }
    text = "Old standing permission, to recreate by hand: " + json.dumps(
        {k: v for k, v in parts.items() if v}, ensure_ascii=False, sort_keys=True
    )
    return shorten(text, EVIDENCE_MAX)


def import_grants(ctx: Context) -> dict[str, str]:
    """Pending approvals become grants. Returns old approval id to new grant handle."""
    handles: dict[str, str] = {}
    finished = _journal_outcomes(ctx.old)
    for approval in sorted(ctx.old.approvals, key=lambda a: (a.kind, a.id)):
        status = _text(approval.details.get("status")).lower() or "approved"
        if status in DEAD_STATUSES:
            key = "approvals already revoked or withdrawn"
            ctx.report.left_behind[key] = ctx.report.left_behind.get(key, 0) + 1
            continue
        ctx.report.where = f"approval {approval.id}"
        handle = derive("grant", approval.kind, approval.id)
        handles[approval.id] = handle
        if writes.get_grant(ctx.db, handle) is not None:
            ctx.report.count("grants", new=False)
            continue
        _insert_grant(ctx, approval, handle, status, approval.id in finished)
    return handles


def _insert_grant(
    ctx: Context, approval: OldApproval, handle: str, status: str, finished: bool
) -> None:
    details = approval.details
    standing = approval.kind == "standing"
    granted = norm_ts(details.get("granted_at"), ctx.zone)
    expires = norm_ts(details.get("expires_at"), ctx.zone)
    if not standing:
        # G-5: a one-off lives 7 days from its grant. No grant date means it is not trusted.
        if granted is None:
            expires = ctx.now
        else:
            horizon = add_days(granted, ONE_OFF_DAYS)
            expires = min(expires, horizon) if expires else horizon
    elif granted is not None:
        cap = add_days(granted, STANDING_DAYS)
        expires = min(expires, cap) if expires else cap
    content = details.get("message_hash") if not standing else details.get("scope_hash")
    card = ctx.imported.cards.get(_text(approval.case_id)) if approval.case_id else None
    writes.insert_grant(
        ctx.db,
        handle=handle,
        kind=GrantKind.STANDING if standing else GrantKind.ONE_OFF,
        source_id=_grant_source(ctx, approval),
        title=_grant_title(approval),
        summary=shorten("Imported from the old harness; confirm it again before use.", 300),
        requirement=Requirement.HUMAN_CONFIRMATION,
        content_sha256=content if is_sha256(content) else sha256_text(f"import\n{approval.id}"),
        scope_json=None,
        card=card if standing and details.get("end_on_case_close") else None,
        supersedes=None,
        proposed_by=ACTOR,
        proposed_at=norm_ts(details.get("recorded_at"), ctx.zone) or granted or ctx.now,
    )
    ended: str | None = None
    if standing:
        state = GrantState.SUSPENDED
        if expires is not None and expires <= ctx.now:
            state, ended = GrantState.EXPIRED, expires
        ctx.report.standing_grants.append(f"{handle} (old {approval.id}): {_grant_title(approval)}")
        review: str | None = ctx.now
    else:
        review = None
        if finished or status in DONE_STATUSES:
            state, ended = GrantState.SPENT, ctx.now
        elif expires is None or expires <= ctx.now:
            state, ended = GrantState.EXPIRED, expires or ctx.now
        else:
            state = GrantState.ACTIVE
    writes.update_grant(
        ctx.db,
        handle,
        GrantState.PROPOSED,
        state=state.value,
        confirmed_via="import",
        granted_at=granted,
        expires_at=expires,
        review_at=review,
        ended_at=ended,
    )
    if standing:
        statement = _evidence_for_standing(ctx, approval, handle)
        if statement is None:
            ctx.report.warnings.append(f"Grant {approval.id}: could not keep its old wording.")
    for old_id in cast(list[Any], details.get("evidence_ids") or [])[
        :EVIDENCE_PER_OPEN_CARD_TARGET
    ]:
        if isinstance(old_id, str):
            add_evidence(ctx.db, ctx.old, old_id, "grant", handle, ctx.zone, ctx.now, ctx.report)
    stored = writes.get_grant(ctx.db, handle)
    ctx.audit.append(
        actor=ACTOR,
        kind="grant.imported",
        subject=handle,
        summary=f"Imported {approval.kind} grant as {state.value}; it needs confirming again.",
        payload_sha256=None if stored is None else stored.content_sha256,
    )
    ctx.report.count("grants", new=True)


def _evidence_for_standing(ctx: Context, approval: OldApproval, grant: str) -> str | None:
    handle = derive("evd", "standing-scope", approval.id)
    if memory.get_evidence(ctx.db, handle) is None:
        memory.insert_evidence(
            ctx.db,
            handle=handle,
            text=_scope_statement(approval),
            ref=None,
            subject_sha256=None,
            session=None,
            now=ctx.now,
        )
        ctx.report.count("evidence", new=True)
    memory.add_evidence_ref(ctx.db, handle, "grant", grant, ctx.now)
    return handle


def import_journals(ctx: Context, grants: dict[str, str]) -> None:
    for journal in sorted(ctx.old.journals, key=lambda j: j.key):
        operation, approval = parse_key(journal.key)
        ctx.report.where = f"journal {journal.key}"
        handle = derive("op", journal.key)
        if writes.get_operation(ctx.db, handle) is not None:
            ctx.report.count("operations", new=False)
            _add_summary(ctx, journal, operation, approval, handle)
            continue
        _insert_operation(ctx, journal, operation, approval, handle, grants)
        _add_summary(ctx, journal, operation, approval, handle)


def _add_summary(
    ctx: Context, journal: OldJournal, operation: str, approval: str, handle: str
) -> None:
    """Keep what the old journal says the write was, for display and matching only.

    It is stored beside the operation, never as an intent: the operation still cannot be sent.
    A second run adds nothing to an operation that already has it.
    """
    kind = _KINDS.get(operation, IntentKind.MAIL_SEND)
    request = _mapping(journal.state.get("request"))
    calendar = kind.value.startswith("calendar.")
    source = _source_for(ctx, request.get("calendar_id") if calendar else None, calendar=calendar)
    summary = summarise(request, source if calendar else None)
    when = attempted_at(
        journal, next((a for a in ctx.old.approvals if a.id == approval), None), ctx.zone
    )
    if summary is None and when is None:
        return
    text = (summary or OperationSummary()).model_dump_json(exclude_none=True)
    if writes.set_operation_summary(ctx.db, handle, text, when):
        ctx.report.count("operation_summaries", new=True)


def _operation_state(journal: OldJournal) -> OperationState:
    stage = journal.state.get("stage")
    if stage in SUCCESS_STAGES:
        return OperationState.CONFIRMED
    if stage in CLEAN_STAGES:
        return OperationState.FAILED_CLEAN
    return OperationState.ABANDONED


def _insert_operation(
    ctx: Context,
    journal: OldJournal,
    operation: str,
    approval: str,
    handle: str,
    grants: dict[str, str],
) -> None:
    kind = _KINDS.get(operation)
    if kind is None:
        kind = IntentKind.MAIL_SEND
        ctx.report.warnings.append(
            f"Journal {journal.key}: unknown operation '{operation}', recorded as a send."
        )
    request = _mapping(journal.state.get("request"))
    calendar = kind.value.startswith("calendar.")
    source = _source_for(
        ctx,
        request.get("calendar_id") if calendar else request.get("mailbox"),
        request.get("mailbox"),
        calendar=calendar,
    )
    fingerprint = journal.state.get("fingerprint")
    intent_hash = fingerprint if is_sha256(fingerprint) else sha256_text(f"import\n{journal.key}")
    state = _operation_state(journal)
    grant = grants.get(approval)
    writes.insert_operation(
        ctx.db,
        handle=handle,
        idempotency_key=f"import:{journal.key}"[:200],
        kind=kind,
        source_id=source,
        card=None,
        session=None,
        intent_sha256=intent_hash,
        intent_json="{}",
        refs_json="{}",
        at=ctx.now,
    )
    left = state is OperationState.ABANDONED
    if left:
        ctx.report.left_with_old_harness.append(f"{handle} ({kind.value}, old {journal.key})")
    receipt = StoredReceipt(
        summary=Receipt(
            intent_kind=kind,
            source=source,
            state=state,
            intent_sha256=intent_hash,
            settled_at=parse(ctx.now),
            verification=Verification(),
            items_ok=1 if state is OperationState.CONFIRMED else 0,
        )
    ).model_dump_json(exclude_none=True)
    moved = writes.update_operation(
        ctx.db,
        handle,
        expected_state=OperationState.PROPOSED,
        state=state.value,
        intent_json=None,
        refs_json=None,
        grant=grant,
        updated_at=ctx.now,
        settled_at=ctx.now,
        receipt_json=receipt,
    )
    if not moved:
        ctx.report.warnings.append(f"Journal {journal.key}: the operation row could not be set.")
    ctx.audit.append(
        actor=ACTOR,
        kind="operation.imported",
        subject=handle,
        summary=f"Imported old {kind.value} journal as {state.value}; never retried."
        + (" It was left with the old harness." if left else ""),
        payload_sha256=intent_hash,
    )
    ctx.report.count("operations", new=True)
