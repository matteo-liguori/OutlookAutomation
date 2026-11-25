"""Old card context: kept as a stored text item under the card, once, and due for review.

Contract: docs/contracts/06-memory.md (M-35). Synthetic old data only.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from old_fixture import BIG_CONTEXT, MemorySecrets, build_old_root

from chamberlain.app.core import App
from chamberlain.app.wiring import COMPACTORS
from chamberlain.connections import open_app
from chamberlain.importer.report import ImportReport
from chamberlain.importer.run import run_import
from chamberlain.limits import IMPORTED_CONTEXT_DAYS
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.memory.sweeper import sweep
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import connect

SOLICITOR = "Solicitor: Example & Co. Ref EX-1234. Fee agreed; engagement letter unsigned. " * 30


def _set_context(old: Path, case_id: str, text: str) -> None:
    db = sqlite3.connect(old / "monitor" / "monitor.db")
    try:
        row = db.execute("SELECT details FROM cases WHERE id = ?", (case_id,)).fetchone()
        details: dict[str, Any] = json.loads(row[0])
        details["context"] = text
        db.execute("UPDATE cases SET details = ? WHERE id = ?", (json.dumps(details), case_id))
        db.commit()
    finally:
        db.close()


def _add_case(old: Path, case_id: str, title: str, context: str, status: str = "open") -> None:
    db = sqlite3.connect(old / "monitor" / "monitor.db")
    try:
        details = json.dumps({"purpose": title, "context": context})
        db.execute(
            "INSERT INTO cases VALUES(?,?,?,?,?,?,?)",
            (case_id, title, "Waiting.", status, 1, "2026-01-02T10:00:00+00:00", details),
        )
        db.commit()
    finally:
        db.close()


def _rows(data_root: Path, sql: str, *args: object) -> list[sqlite3.Row]:
    db = connect(data_root / "chamberlain.sqlite3")
    try:
        return list(db.execute(sql, args))
    finally:
        db.close()


@pytest.fixture
def old_root(tmp_path: Path) -> Path:
    root = build_old_root(tmp_path / "old")
    _add_case(root, "case-ctx", "Solicitor matter", SOLICITOR)
    _add_case(root, "case-blank", "Blank context", "  \n ")
    return root


@pytest.fixture
def secrets() -> MemorySecrets:
    return MemorySecrets()


def _start(app: App) -> str:
    started = app.call("start", {"agent": "test"})
    assert started.ok
    return str(started.data.session)  # type: ignore[union-attr]


def _context_items(data_root: Path) -> list[sqlite3.Row]:
    return _rows(
        data_root,
        "SELECT a.*, o.owner AS card FROM artifacts a "
        "JOIN artifact_owners o ON o.artifact = a.handle AND o.owner_kind = 'card' "
        "ORDER BY o.owner",
    )


def test_m35_open_cards_with_context_get_one_card_owned_and_1_related_checks(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    # m35_open_cards_with_context_get_one_card_owned_text_item
    report = run_import(old_root, data_root, clock=clock, secrets=secrets)
    items = _context_items(data_root)
    titles = {r["handle"]: r["title"] for r in _rows(data_root, "SELECT handle, title FROM cards")}
    assert sorted(titles[i["card"]] for i in items) == ["Oversized card", "Solicitor matter"]
    assert report.counts["context_artifacts"] == 2  # not the closed card, not the blank one
    solicitor = next(i for i in items if titles[i["card"]] == "Solicitor matter")
    assert solicitor["kind"] == "text"
    assert solicitor["state"] == "live"
    assert solicitor["filename"] == "context-from-old-harness.txt"
    assert solicitor["reason"].startswith("Context from the old harness")
    assert solicitor["size"] == len(SOLICITOR.strip().encode())
    big = next(i for i in items if titles[i["card"]] == "Oversized card")
    assert big["size"] == len(BIG_CONTEXT)  # the old 6,000 character limit, nothing cut
    expires = clock.now().timestamp() + IMPORTED_CONTEXT_DAYS * 86400
    assert abs(_stamp(solicitor["expires_at"]) - expires) < 5

    # m35_the_card_is_due_for_review_now_and_the_agent_can_read_the_item
    run_import(old_root, data_root, clock=clock, secrets=secrets)
    card = _rows(data_root, "SELECT * FROM cards WHERE title = 'Solicitor matter'")[0]
    assert card["review_at"] == "2026-01-05T09:00:00Z"  # now, not the old review clock
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        session = _start(app)
        opened = app.call("open", {"handle": card["handle"], "session": session})
        view = opened.data.view  # type: ignore[union-attr]
        assert view.context is not None
        rule = next(g for g in opened.guidance if g.id == "card.context")
        text = rule.text or ""
        assert "forget" in text
        assert str(IMPORTED_CONTEXT_DAYS) in text  # the lifetime is stated
        item = app.call("open", {"handle": view.context, "part": "body", "session": session})
        assert item.ok
        assert "EX-1234" in item.data.content.text  # type: ignore[union-attr]
    finally:
        app.close()


def _stamp(text: str) -> float:
    from datetime import UTC, datetime

    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def test_m35_an_import_into_a_store_that_has_the_cards_adds_the_missing_context_once(
    tmp_path: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    """The owner's live store was imported before context was carried over."""
    old = build_old_root(tmp_path / "old")
    _set_context(old, "case-big", "")
    _add_case(old, "case-ctx", "Solicitor matter", "")
    first = run_import(old, data_root, clock=clock, secrets=secrets)
    assert "context_artifacts" not in first.counts
    assert _context_items(data_root) == []
    _set_context(old, "case-big", BIG_CONTEXT)
    _set_context(old, "case-ctx", SOLICITOR)
    clock.advance(hours=3)
    second = run_import(old, data_root, clock=clock, secrets=secrets)
    assert second.counts == {"context_artifacts": 2}
    assert second.document()["counts"] == {"context_artifacts": 2}
    assert len(_context_items(data_root)) == 2
    due = _rows(data_root, "SELECT review_at FROM cards WHERE title = 'Solicitor matter'")[0]
    assert due["review_at"] == "2026-01-05T12:00:00Z"
    third = run_import(old, data_root, clock=clock, secrets=secrets)
    assert not third.changed
    assert len(_context_items(data_root)) == 2


def test_m35_forgetting_the_item_is_final_a_later_import_does_not_bring_it_back(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    run_import(old_root, data_root, clock=clock, secrets=secrets)
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        session = _start(app)
        handle = _context_items(data_root)[0]["handle"]
        assert app.call("forget", {"handle": handle, "session": session}).ok
    finally:
        app.close()
    again = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert not again.changed


def test_m35_unread_context_expires_on_its_own_and_the_card_and_1_related_checks(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    # m35_unread_context_expires_on_its_own_and_the_card_stops_showing_it
    run_import(old_root, data_root, clock=clock, secrets=secrets)
    card = _rows(data_root, "SELECT handle FROM cards WHERE title = 'Solicitor matter'")[0][0]
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        clock.advance(hours=(IMPORTED_CONTEXT_DAYS + 1) * 24)
        sweep(app.context, clock.now(), compactors=COMPACTORS)
        left = app.context.db.execute(
            "SELECT count(*) FROM artifacts WHERE state = 'live' AND filename LIKE 'context-%'"
        ).fetchone()[0]
        assert left == 0
        session = _start(app)
        view = app.call("open", {"handle": card, "session": session}).data.view  # type: ignore[union-attr]
        assert view.context is None
    finally:
        app.close()

    # m35_a_card_the_owner_closed_gets_no_context_on_a_later_import
    _set_context(old_root, "case-ctx", "")
    run_import(old_root, data_root, clock=clock, secrets=secrets)
    handle = _rows(data_root, "SELECT handle FROM cards WHERE title = 'Solicitor matter'")[0][0]
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        session = _start(app)
        closed = app.call(
            "card", {"op": "close", "card": handle, "outcome": "Done.", "session": session}
        )
        assert closed.ok
    finally:
        app.close()
    _set_context(old_root, "case-ctx", SOLICITOR)
    again = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert "context_artifacts" not in again.counts


def test_m35_closing_the_card_releases_its_context_and_1_related_checks(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    # m35_closing_the_card_releases_its_context
    run_import(old_root, data_root, clock=clock, secrets=secrets)
    handle = _rows(data_root, "SELECT handle FROM cards WHERE title = 'Solicitor matter'")[0][0]
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        session = _start(app)
        closed = app.call(
            "card", {"op": "close", "card": handle, "outcome": "Done.", "session": session}
        )
        assert closed.ok
        state = app.context.db.execute(
            "SELECT a.state FROM artifacts a JOIN imported_context i ON i.artifact = a.handle "
            "WHERE i.card = ?",
            (handle,),
        ).fetchone()[0]
        assert state == "released"
    finally:
        app.close()

    # m35_the_agent_cannot_store_things_directly_under_a_card
    run_import(old_root, data_root, clock=clock, secrets=secrets)
    handle = _rows(data_root, "SELECT handle FROM cards WHERE title = 'Solicitor matter'")[0][0]
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        store = ArtifactService(app.context.db, clock, data_root)
        with pytest.raises(ChamberlainError) as error:
            store.keep_bytes(data=b"x", kind=_text_kind(), owner=handle, reason="no")
        assert error.value.code is ErrorCode.ARTIFACT_NO_OWNER
    finally:
        app.close()


def _text_kind() -> Any:
    from chamberlain.domain.artifacts import ArtifactKind

    return ArtifactKind.TEXT


def test_m35_the_import_report_names_the_count_and_1_related_checks(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    # m35_the_import_report_names_the_count
    report: ImportReport = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert report.document()["counts"]["context_artifacts"] == 2
    saved = json.loads((data_root / "import" / "report.json").read_text(encoding="utf-8"))
    assert saved["counts"]["context_artifacts"] == 2

    # m35_a_card_with_old_context_is_not_offered_for_closing: The review event says `context`, s
    from chamberlain.attention.collect import collect
    from chamberlain.attention.detectors.due import card_review_due
    from chamberlain.attention.rules import load_rules

    run_import(old_root, data_root, clock=clock, secrets=secrets)
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        by_title = {
            r["handle"]: r["title"]
            for r in app.context.db.execute("SELECT handle, title FROM cards")
        }
        events = {by_title[e.subject or ""]: e for e in card_review_due(collect(app.context).data)}
        assert events["Solicitor matter"].attrs.reason == "context"
        assert "old system" in events["Solicitor matter"].summary
        assert events["Oversized card"].attrs.reason == "over_cap"  # trimming comes first
        review = next(r for r in load_rules() if r.id == "card.review")
        assert review.trigger is not None
        assert ("reason", ("review",)) in review.trigger.filters
        # once the agent has forgotten the notes, the ordinary review comes back
        session = _start(app)
        card = next(h for h, t in by_title.items() if t == "Solicitor matter")
        item = repo_item(app, card)
        assert app.call("forget", {"handle": item, "session": session}).ok
        after = {by_title[e.subject or ""]: e for e in card_review_due(collect(app.context).data)}
        assert after["Solicitor matter"].attrs.reason == "review"
    finally:
        app.close()


def repo_item(app: App, card: str) -> str:
    row = app.context.db.execute(
        "SELECT artifact FROM imported_context WHERE card = ?", (card,)
    ).fetchone()
    return str(row[0])
