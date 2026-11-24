"""Cards, actions and evidence from the old harness, written through memory's repository.

Contract: docs/rebuild-plan.md ("Migrating current users' private data") and
06-memory.md (M-13: an imported card over a cap is stored as it is, flagged, and due for
review). Open cards keep their review clocks; closed cards keep one outcome line; evidence
comes across only when an imported card refers to it, newest 15 per card (M-12 target).
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from chamberlain.domain.artifacts import ArtifactKind
from chamberlain.domain.cards import card_over_cap
from chamberlain.importer.convert import add_days, derive, norm_ts, shorten
from chamberlain.importer.old import Json, OldAction, OldCase, OldInstallation
from chamberlain.importer.report import ImportReport
from chamberlain.limits import (
    ACTION_TITLE_MAX,
    CARD_GOAL_MAX,
    CARD_OPEN_ACTIONS_MAX,
    CARD_OUTCOME_DAYS,
    CARD_POSITION_MAX,
    CARD_RULES_MAX,
    CARD_TITLE_MAX,
    EVIDENCE_MAX,
    EVIDENCE_PER_OPEN_CARD_TARGET,
    IMPORTED_CONTEXT_DAYS,
    OUTCOME_MAX,
)
from chamberlain.memory import repo as memory
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.schema.tools import ActionView, CardView

OPEN_ACTION = frozenset({"needs-user", "waiting", "watch"})
DEFAULT_REVIEW_DAYS = 7
CLOSED_KEEP_DAYS = 30
"""A closed card imported near its purge date is kept at least this long."""
CONTEXT_FILENAME = "context-from-old-harness.txt"
CONTEXT_REASON = (
    "Context from the old harness. Fold what still matters into the position, then forget this."
)


@dataclass
class Imported:
    """Handles of what exists in the new store for each old record that was imported."""

    cards: dict[str, str] = field(default_factory=dict[str, str])
    evidence: dict[str, str] = field(default_factory=dict[str, str])


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _evidence_text(old_body: str, meta: Json) -> str:
    label = _text(meta.get("label")) or _text(meta.get("kind")) or _text(meta.get("source"))
    text = f"{label}: {old_body}" if label and old_body else (old_body or label or "Imported")
    return shorten(" ".join(text.split()), EVIDENCE_MAX)


def add_evidence(
    db: sqlite3.Connection,
    old: OldInstallation,
    old_id: str,
    owner_kind: str,
    owner: str,
    zone: ZoneInfo,
    now: str,
    report: ImportReport,
) -> str | None:
    """Store one old evidence row once and link it to `owner`. None when the old row is gone."""
    row = old.evidence.get(old_id)
    if row is None:
        return None
    handle = derive("evd", old_id)
    is_new = memory.get_evidence(db, handle) is None
    if is_new:
        created = (
            norm_ts(row.metadata.get("received"), zone)
            or norm_ts(row.metadata.get("captured_at"), zone)
            or norm_ts(row.metadata.get("at"), zone)
            or now
        )
        memory.insert_evidence(
            db,
            handle=handle,
            text=_evidence_text(row.body, row.metadata),
            ref=None,
            subject_sha256=None,
            session=None,
            now=created,
        )
        report.count("evidence", new=True)
    memory.add_evidence_ref(db, handle, owner_kind, owner, now)
    return handle


def _evidence_ids(case: OldCase, actions: list[OldAction]) -> list[str]:
    """Every evidence id a card refers to, newest first, no repeats."""
    found: dict[str, str] = {}
    index = case.details.get("evidence_index")
    for entry in cast(list[Any], index) if isinstance(index, list) else []:
        if isinstance(entry, dict):
            entry_map = cast(Json, entry)
            if isinstance(entry_map.get("id"), str):
                found[entry_map["id"]] = str(entry_map.get("at") or "")
    for key in ("evidence_ids",):
        for item in cast(list[Any], case.details.get(key) or []):
            if isinstance(item, str):
                found.setdefault(item, "")
    for action in actions:
        for key in ("completion_evidence", "evidence_ids"):
            for item in cast(list[Any], action.details.get(key) or []):
                if isinstance(item, str):
                    found.setdefault(item, "")
    return [
        eid for eid, _ in sorted(found.items(), key=lambda pair: (pair[1], pair[0]), reverse=True)
    ]


def _card_fields(case: OldCase) -> tuple[str, str, str, str]:
    purpose = _text(case.details.get("purpose"))
    goal = "" if purpose == case.title.strip() else purpose
    return case.title.strip(), goal, case.position.strip(), ""


def _review_at(case: OldCase, zone: ZoneInfo, now: str) -> str:
    reviewed = norm_ts(case.details.get("card_reviewed_at"), zone)
    interval = case.details.get("review_interval_days")
    days = interval if isinstance(interval, int) and interval >= 1 else DEFAULT_REVIEW_DAYS
    if reviewed is None:
        return now
    return add_days(reviewed, days)


def _over_cap(fields: tuple[str, str, str, str], actions: list[ActionView], handle: str) -> bool:
    title, goal, position, rules = fields
    if (
        len(title) > CARD_TITLE_MAX
        or len(goal) > CARD_GOAL_MAX
        or len(position) > CARD_POSITION_MAX
        or len(rules) > CARD_RULES_MAX
        or len(actions) > CARD_OPEN_ACTIONS_MAX
    ):
        return True
    view = CardView(
        handle=handle,
        title=title,
        goal=goal,
        position=position,
        rules=rules,
        state="open",
        version=1,
        actions=tuple(actions),
    )
    return card_over_cap(view)


def import_context(
    db: sqlite3.Connection,
    artifacts: ArtifactService,
    case: OldCase,
    handle: str,
    now: str,
    report: ImportReport,
) -> None:
    """Keep the old free-text context under the card, once, and make the card due for review.

    Safe to repeat: the ledger row (`imported_context`) outlives the stored item, so a second
    import never adds it again, even after the agent folded it into the position and forgot it.
    """
    text = _text(case.details.get("context"))
    if not text or case.status == "closed" or memory.imported_context_done(db, case.id):
        return
    card = memory.get_card(db, handle)
    if card is None or str(card["state"]) != "open":
        return
    kept = artifacts.keep_bytes(
        data=text.encode("utf-8"),
        kind=ArtifactKind.TEXT,
        owner=handle,
        reason=CONTEXT_REASON,
        filename=CONTEXT_FILENAME,
        media_type="text/plain",
        expires_in_hours=IMPORTED_CONTEXT_DAYS * 24,
        internal=True,
    )
    memory.record_imported_context(db, case.id, handle, kept.handle, now)
    memory.set_card_review(db, handle, now, bool(card["over_cap"]))
    report.count("context_artifacts", new=True)


def import_cards(
    db: sqlite3.Connection,
    old: OldInstallation,
    zone: ZoneInfo,
    now: str,
    report: ImportReport,
    artifacts: ArtifactService,
) -> Imported:
    imported = Imported()
    by_case: dict[str, list[OldAction]] = {}
    for action in old.actions:
        if action.status in OPEN_ACTION:
            by_case.setdefault(action.case_id, []).append(action)
    for case in sorted(old.cases, key=lambda c: c.id):
        report.where = f"case {case.id}"
        handle = derive("card", case.id)
        imported.cards[case.id] = handle
        if memory.get_card(db, handle) is not None:
            report.count("cards", new=False)
            import_context(db, artifacts, case, handle, now, report)
            continue
        report.count("cards", new=True)
        if case.status == "closed":
            _import_closed(db, case, handle, zone, now)
            continue
        actions = sorted(by_case.get(case.id, []), key=lambda a: a.id)
        fields = _card_fields(case)
        views: list[ActionView] = []
        for action in actions:
            title = shorten(_text(action.details.get("next_step")) or action.id, ACTION_TITLE_MAX)
            due = norm_ts(action.next_review, zone)
            views.append(
                ActionView(
                    handle=derive("act", action.id),
                    card=handle,
                    title=title,
                    state="open",
                    due_at=None if due is None else _parse(due),
                )
            )
        over = _over_cap(fields, views, handle)
        memory.insert_card(
            db,
            handle=handle,
            title=fields[0],
            goal=fields[1],
            position=fields[2],
            rules=fields[3],
            over_cap=over,
            review_at=now if over else _review_at(case, zone, now),
            now=now,
        )
        if over:
            report.over_cap_cards.append(f"{handle} (old {case.id})")
        for action in actions:
            _import_action(db, action, handle, zone, now, report)
        import_context(db, artifacts, case, handle, now, report)
        wanted = _evidence_ids(case, actions)
        if len(wanted) > EVIDENCE_PER_OPEN_CARD_TARGET:
            key = f"evidence beyond {EVIDENCE_PER_OPEN_CARD_TARGET} per open card"
            extra = len(wanted) - EVIDENCE_PER_OPEN_CARD_TARGET
            report.left_behind[key] = report.left_behind.get(key, 0) + extra
        for old_id in wanted[:EVIDENCE_PER_OPEN_CARD_TARGET]:
            linked = add_evidence(db, old, old_id, "card", handle, zone, now, report)
            if linked is not None:
                imported.evidence[old_id] = linked
    return imported


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))


def _import_closed(
    db: sqlite3.Connection, case: OldCase, handle: str, zone: ZoneInfo, now: str
) -> None:
    closed_at = norm_ts(case.updated_at, zone) or now
    purge_at = max(add_days(closed_at, CARD_OUTCOME_DAYS), add_days(now, CLOSED_KEEP_DAYS))
    memory.insert_card(
        db,
        handle=handle,
        title=case.title.strip(),
        goal="",
        position="",
        rules="",
        over_cap=False,
        review_at=None,
        now=closed_at,
    )
    outcome = shorten(" ".join((case.position or case.title).split()), OUTCOME_MAX)
    memory.close_card(db, handle, outcome, closed_at, purge_at)


def _import_action(
    db: sqlite3.Connection,
    action: OldAction,
    card: str,
    zone: ZoneInfo,
    now: str,
    report: ImportReport,
) -> None:
    title = _text(action.details.get("next_step")) or action.id
    if len(title) > ACTION_TITLE_MAX:
        report.warnings.append(f"Action {action.id}: title cut to {ACTION_TITLE_MAX} characters.")
    snooze = norm_ts(action.details.get("snooze_until"), zone)
    memory.insert_action(
        db,
        handle=derive("act", action.id),
        card=card,
        title=shorten(title, ACTION_TITLE_MAX),
        due_at=norm_ts(action.next_review, zone),
        snooze_until=snooze,
        now=now,
    )
    report.count("actions", new=True)
