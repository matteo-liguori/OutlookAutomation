"""The owner's own rules, card knowledge and appointment flags come across, once.

Contract: docs/contracts/06-memory.md (M-41), 07-attention.md (A-28), 05-grants.md (G-24).
Scenario: an older import (before these were carried) exists; the old data gains the owner's
knowledge; v2 is changed by the agent; a second import adds what is missing and touches nothing
the agent changed; a third import does nothing. Synthetic data only.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from old_fixture import MemorySecrets, add_owner_knowledge, build_old_root

from chamberlain.attention.overrides import apply_overrides
from chamberlain.attention.rules import load_rules
from chamberlain.attention.selection import Situation, select
from chamberlain.connections.load import load_connections, profile_path
from chamberlain.importer.run import run_import
from chamberlain.ports.clock import ManualClock
from chamberlain.store.db import connect


def _db(data_root: Path) -> sqlite3.Connection:
    return connect(data_root / "chamberlain.sqlite3")


def _one(data_root: Path, sql: str, *args: object) -> Any:
    db = _db(data_root)
    try:
        return db.execute(sql, args).fetchone()
    finally:
        db.close()


def _all(data_root: Path, sql: str, *args: object) -> list[sqlite3.Row]:
    db = _db(data_root)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def _snapshot(data_root: Path) -> str:
    tables = ("cards", "actions", "grants", "imported_items")
    return json.dumps(
        [[tuple(r) for r in _all(data_root, f"SELECT * FROM {t}")] for t in tables], default=str
    )


def _run(old: Path, data_root: Path, clock: ManualClock, secrets: MemorySecrets) -> Any:
    return run_import(old, data_root, clock=clock, secrets=secrets, seed_recipients=False)


def _as_the_live_root_was(data_root: Path) -> None:
    """The live root was imported before any of this existed: no ledger, no owner file, no
    read_state section in the profile."""
    db = _db(data_root)
    try:
        db.execute("DELETE FROM imported_items")
        db.commit()
    finally:
        db.close()
    (data_root / "config" / "owner-instructions.yaml").unlink(missing_ok=True)
    path = profile_path(data_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(r"^read_state:\n(  .*\n)+", "", text, flags=re.MULTILINE), encoding="utf-8"
    )


def test_a_second_import_adds_the_owners_knowledge_and_keeps_what_v2_changed(
    tmp_path: Path, data_root: Path, clock: ManualClock
) -> None:
    secrets = MemorySecrets()
    old = build_old_root(tmp_path / "old")
    _run(old, data_root, clock, secrets)
    add_owner_knowledge(old)
    _as_the_live_root_was(data_root)
    # v2 moves on: the agent writes its own rules on one card and reschedules one action.
    db = _db(data_root)
    try:
        db.execute("UPDATE cards SET rules = 'Agent wrote this' WHERE title = 'Oversized card'")
        db.execute("UPDATE actions SET due_at = '2026-02-01T09:00:00Z' WHERE title = 'Step act-2'")
        db.commit()
    finally:
        db.close()
    clock.advance(hours=1)

    report = _run(old, data_root, clock, secrets)

    # Card rules: the owner's instructions then the decision, verbatim; the agent's rules stay.
    alpha = _one(data_root, "SELECT rules FROM cards WHERE title = 'Alpha supplier contract'")
    assert alpha["rules"] == (
        "supplier: Always copy the finance team on supplier mail.\n"
        "Decided: Pay by bank transfer, never card."
    )
    big = _one(data_root, "SELECT rules FROM cards WHERE title = 'Oversized card'")
    assert big["rules"] == "Agent wrote this"
    assert any("rules" in item for item in report.kept_as_changed)

    # Important appointments: due at the start, reminder from the old reminder time; the action
    # the agent rescheduled is left alone; a brand new one arrives flagged.
    flagged = {r["title"]: r for r in _all(data_root, "SELECT * FROM actions WHERE important = 1")}
    assert set(flagged) == {"Step act-1", "Passport visit"}
    assert flagged["Step act-1"]["due_at"] == "2026-03-01T10:00:00Z"
    assert flagged["Step act-1"]["snooze_until"] == "2026-02-28T09:00:00Z"
    assert flagged["Passport visit"]["due_at"] == "2026-04-01T08:30:00Z"
    untouched = _one(data_root, "SELECT important, due_at FROM actions WHERE title = 'Step act-2'")
    assert (untouched["important"], untouched["due_at"]) == (0, "2026-02-01T09:00:00Z")
    assert any("changed since" in item for item in report.kept_as_changed)

    # Owner instructions: only what differs from the old shipped text; facts and watches too.
    topics = yaml.safe_load(
        (data_root / "config" / "owner-instructions.yaml").read_text(encoding="utf-8")
    )["topics"]
    assert set(topics) == {"filing", "reporting", "standing-facts", "keyword-watches"}
    assert "outlook" in report.old_default_instructions
    assert topics["standing-facts"]["text"] == "- Acme Ltd: Our main supplier; contact is Sam."
    assert "tenancy or deposit" in topics["keyword-watches"]["text"]

    # The agent receives them: one rule per place, a long group only as a pointer.
    rules = apply_overrides(load_rules(), data_root).rules
    mail = next(r for r in rules if r.id == "owner.mail")
    assert "Private filing rule: file receipts under Finance." in mail.text
    assert "tenancy or deposit" in mail.text
    start = next(r for r in rules if r.id == "owner.start")
    assert "Acme Ltd" in start.text
    situation = Situation("start", {}, {}, None, None, ())
    assert [c.rule.id for c in select((start,), situation, {}).full] == ["owner.start"]
    long_text = start.model_copy(update={"text": "x" * 700})
    pointed = select((long_text,), situation, {})
    assert pointed.full == ()
    assert [r.id for r in pointed.pointers] == ["owner.start"]

    # The owner's v1 read-state settings are on: profile options, and one proposed grant per
    # mailbox that only a click can confirm.
    state = load_connections(data_root).read_state
    assert state.filing_marks_read
    assert state.reporting_marks_read
    grants = _all(
        data_root,
        "SELECT state, requirement, proposed_by FROM grants WHERE title LIKE 'Mark mail read%'",
    )
    assert len(grants) == 1
    assert {(g["state"], g["requirement"], g["proposed_by"]) for g in grants} == {
        ("proposed", "human_confirmation", "system:read_state")
    }

    # The owner edits and deletes in v2; a third import adds nothing and changes nothing.
    path = data_root / "config" / "owner-instructions.yaml"
    edited = yaml.safe_load(path.read_text(encoding="utf-8"))
    edited["topics"]["filing"]["text"] = "Owner edited this."
    del edited["topics"]["reporting"]
    path.write_text(yaml.safe_dump(edited), encoding="utf-8")
    before = _snapshot(data_root)
    third = _run(old, data_root, clock, secrets)
    after = _snapshot(data_root)
    assert before == after
    assert path.read_text(encoding="utf-8") == yaml.safe_dump(edited)
    assert not third.changed
    # The earlier report is kept beside the newer one.
    assert (data_root / "import" / "report.1.json").is_file()
