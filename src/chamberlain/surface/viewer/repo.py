"""All SQL of the viewer (S-4): read-only views of other tracks' tables, and nothing else.

Contract: docs/contracts/09-storage.md (S-3: other tracks' tables are only read). The viewer
writes through the services (`ConfirmationApp`, the runs track's notes), never here.
"""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class CardLine:
    handle: str
    title: str
    review_at: str | None
    over_cap: bool
    open_actions: int


@dataclass(frozen=True)
class ActionLine:
    handle: str
    card: str
    card_title: str
    title: str
    due_at: str | None
    snooze_until: str | None


@dataclass(frozen=True)
class AuditLine:
    seq: int
    at: str
    actor: str
    kind: str
    subject: str | None
    summary: str


def open_cards(db: sqlite3.Connection, limit: int) -> list[CardLine]:
    rows = db.execute(
        "SELECT c.handle, c.title, c.review_at, c.over_cap, "
        "(SELECT count(*) FROM actions a WHERE a.card = c.handle AND a.state = 'open') AS n "
        "FROM cards c WHERE c.state = 'open' ORDER BY c.updated_at DESC, c.handle LIMIT ?",
        (limit,),
    )
    return [
        CardLine(r["handle"], r["title"], r["review_at"], bool(r["over_cap"]), int(r["n"]))
        for r in rows
    ]


def count_open_cards(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT count(*) FROM cards WHERE state = 'open'").fetchone()[0])


def due_actions(db: sqlite3.Connection, until: str, limit: int) -> list[ActionLine]:
    """Open actions due by `until` (a stored timestamp), soonest first."""
    rows = db.execute(
        "SELECT a.handle, a.card, c.title AS card_title, a.title, a.due_at, a.snooze_until "
        "FROM actions a JOIN cards c ON c.handle = a.card "
        "WHERE a.state = 'open' AND a.due_at IS NOT NULL AND a.due_at <= ? "
        "ORDER BY a.due_at, a.handle LIMIT ?",
        (until, limit),
    )
    return [
        ActionLine(
            r["handle"], r["card"], r["card_title"], r["title"], r["due_at"], r["snooze_until"]
        )
        for r in rows
    ]


def audit_between(db: sqlite3.Connection, start: str, end: str, limit: int) -> list[AuditLine]:
    """Audit rows from `start` up to (not including) `end`, newest first, excluding tool calls."""
    rows = db.execute(
        "SELECT seq, at, actor, kind, subject, summary FROM audit_log "
        "WHERE at >= ? AND at < ? AND kind != 'tool.call' ORDER BY seq DESC LIMIT ?",
        (start, end, limit),
    )
    return [_audit(r) for r in rows]


def tool_call_summaries(db: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = db.execute(
        "SELECT summary FROM audit_log WHERE at >= ? AND at < ? AND kind = 'tool.call'",
        (start, end),
    )
    return [str(r["summary"]) for r in rows]


def _audit(row: sqlite3.Row) -> AuditLine:
    return AuditLine(
        row["seq"], row["at"], row["actor"], row["kind"], row["subject"], row["summary"]
    )


def count_live_events(db: sqlite3.Connection) -> int:
    return int(
        db.execute("SELECT count(*) FROM events WHERE state IN ('new', 'delivered')").fetchone()[0]
    )


def over_budget_flag(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT value FROM system_state WHERE key = 'memory.over_budget'").fetchone()
    return "0" if row is None else str(row["value"])


@dataclass(frozen=True)
class CardDetail:
    handle: str
    title: str
    goal: str
    position: str
    review_at: str | None
    over_cap: bool
    updated_at: str


@dataclass(frozen=True)
class OpenAction:
    handle: str
    card: str
    title: str
    due_at: str | None
    snooze_until: str | None


@dataclass(frozen=True)
class ClosedAction:
    handle: str
    card: str
    card_title: str
    title: str
    state: str
    outcome: str | None
    closed_at: str


def card_details(db: sqlite3.Connection, limit: int) -> list[CardDetail]:
    rows = db.execute(
        "SELECT handle, title, goal, position, review_at, over_cap, updated_at FROM cards "
        "WHERE state = 'open' ORDER BY updated_at DESC, handle LIMIT ?",
        (limit,),
    )
    return [
        CardDetail(
            r["handle"],
            r["title"],
            r["goal"],
            r["position"],
            r["review_at"],
            bool(r["over_cap"]),
            r["updated_at"],
        )
        for r in rows
    ]


def open_actions_of(db: sqlite3.Connection, cards: list[str]) -> list[OpenAction]:
    """Open actions of the given cards, the soonest due first (those with no date last)."""
    if not cards:
        return []
    marks = ", ".join("?" for _ in cards)
    rows = db.execute(
        "SELECT handle, card, title, due_at, snooze_until FROM actions "
        f"WHERE state = 'open' AND card IN ({marks}) "
        "ORDER BY due_at IS NULL, due_at, handle",
        cards,
    )
    return [
        OpenAction(r["handle"], r["card"], r["title"], r["due_at"], r["snooze_until"]) for r in rows
    ]


def dated_actions(db: sqlite3.Connection, start: str, end: str, limit: int) -> list[ActionLine]:
    """Open actions due in `[start, end)`, for the calendar's important dates."""
    rows = db.execute(
        "SELECT a.handle, a.card, c.title AS card_title, a.title, a.due_at, a.snooze_until "
        "FROM actions a JOIN cards c ON c.handle = a.card "
        "WHERE a.state = 'open' AND a.due_at >= ? AND a.due_at < ? "
        "ORDER BY a.due_at, a.handle LIMIT ?",
        (start, end, limit),
    )
    return [
        ActionLine(
            r["handle"], r["card"], r["card_title"], r["title"], r["due_at"], r["snooze_until"]
        )
        for r in rows
    ]


def closed_actions_between(
    db: sqlite3.Connection, start: str, end: str, limit: int
) -> list[ClosedAction]:
    rows = db.execute(
        "SELECT a.handle, a.card, c.title AS card_title, a.title, a.state, a.outcome, a.closed_at "
        "FROM actions a JOIN cards c ON c.handle = a.card "
        "WHERE a.state IN ('done', 'dropped') AND a.closed_at >= ? AND a.closed_at < ? "
        "ORDER BY a.closed_at DESC, a.handle LIMIT ?",
        (start, end, limit),
    )
    return [
        ClosedAction(
            r["handle"],
            r["card"],
            r["card_title"],
            r["title"],
            r["state"],
            r["outcome"],
            r["closed_at"],
        )
        for r in rows
    ]


def source_labels(db: sqlite3.Connection) -> dict[str, str]:
    rows = db.execute("SELECT id, label FROM sources")
    return {str(r["id"]): str(r["label"]) for r in rows}
