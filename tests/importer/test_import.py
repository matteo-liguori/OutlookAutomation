"""The importer: docs/rebuild-plan.md "Migrating current users' private data", G-6, M-13, S-14.

Every test imports a synthetic old root built by `old_fixture.py`, never real data.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from old_fixture import MemorySecrets

from chamberlain.connections import load_connections, open_app
from chamberlain.importer.report import ImportReport
from chamberlain.importer.run import run_import
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import connect
from chamberlain.writes.confirm import keep_grant


def fingerprint(root: Path) -> dict[str, str]:
    """Every file under a folder, by relative name, with the hash of its bytes."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def dump_store(data_root: Path) -> dict[str, list[tuple[Any, ...]]]:
    db = connect(data_root / "chamberlain.sqlite3")
    try:
        tables = [
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row["name"].startswith("sqlite_")
        ]
        return {
            name: [tuple(r) for r in db.execute(f"SELECT * FROM {name} ORDER BY 1, 2")]
            for name in sorted(tables)
        }
    finally:
        db.close()


def rows(data_root: Path, sql: str, *args: object) -> list[sqlite3.Row]:
    db = connect(data_root / "chamberlain.sqlite3")
    try:
        return list(db.execute(sql, args))
    finally:
        db.close()


# --- the old installation is never changed ------------------------------------------


def test_s14_the_old_root_is_untouched_by_an_import_and_3_related_checks(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    # s14_the_old_root_is_untouched_by_an_import
    before = fingerprint(old_root)
    assert not [name for name in before if name.endswith(("-wal", "-shm"))]
    run_import(old_root, data_root, clock=clock, secrets=secrets)
    run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert fingerprint(old_root) == before

    # s14_an_old_database_with_unmerged_wal_content_is_refused
    holder = sqlite3.connect(old_root / "monitor" / "monitor.db")
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("INSERT INTO meta VALUES('late', '1')")
    holder.commit()
    try:
        assert (old_root / "monitor" / "monitor.db-wal").stat().st_size > 0
        before = fingerprint(old_root)
        with pytest.raises(ChamberlainError) as raised:
            run_import(old_root, data_root, clock=clock, secrets=secrets)
        assert raised.value.code is ErrorCode.CONFIG_INVALID
        assert fingerprint(old_root) == before
    finally:
        holder.close()

    # s14_an_unsupported_old_schema_is_refused
    db = sqlite3.connect(old_root / "monitor" / "monitor.db")
    db.execute("PRAGMA user_version=4")
    db.close()
    with pytest.raises(ChamberlainError) as raised:
        run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert "schema 4" in raised.value.message

    # s14_the_new_root_may_not_sit_inside_the_old_one
    with pytest.raises(ChamberlainError):
        run_import(old_root, old_root / "new", clock=clock, secrets=secrets)
    with pytest.raises(ChamberlainError):
        run_import(old_root, old_root, clock=clock, secrets=secrets)


# --- idempotency --------------------------------------------------------------------


def test_s14_a_second_import_changes_nothing(
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
    imported: ImportReport,
) -> None:
    assert imported.changed
    store_before = dump_store(data_root)
    files_before = fingerprint(data_root)
    secrets_before = dict(secrets.items)
    clock.advance(hours=3)
    again = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert not again.changed
    assert again.counts == {}
    assert dump_store(data_root) == store_before  # every table, including audit_log
    assert fingerprint(data_root) == files_before  # profile and report included
    assert secrets.items == secrets_before


def test_g_the_profile_becomes_a_valid_connection_profile(
    data_root: Path, imported: ImportReport
) -> None:
    connections = load_connections(data_root)
    assert connections.timezone == "Europe/London"
    assert connections.write_policy.send_known.value == "human_confirmation"
    assert connections.write_policy.other.value == "agent_evidence"
    work = connections.sources["mail:work"].model_dump()
    assert work["kind"] == "thunderbird"
    assert (work["account_id"], work["inbox_folder_id"]) == ("account1", "account1:/INBOX")
    assert work["draft_folder_id"] == "account1:/Drafts"
    assert connections.sources["contacts:work"].model_dump()["address_book_ids"] == ("book1",)
    assert connections.sources["calendar:work"].model_dump()["calendar_id"] == "owl-cal-1"


def test_g_a_transport_google_binding_becomes_a_google_source(
    data_root: Path, imported: ImportReport
) -> None:
    google = load_connections(data_root).sources["calendar:primary"].model_dump()
    assert google["kind"] == "google"
    assert (google["account"], google["calendar_id"]) == ("owner@gmail.example", "primary")


def test_g_an_unbindable_source_is_skipped_and_listed_never_guessed(
    data_root: Path, imported: ImportReport
) -> None:
    sources = load_connections(data_root).sources
    assert "mail:home" not in sources  # its old binding lacks three folder ids
    assert "mail:old-com" not in sources  # a COM binding is dropped
    assert "calendar:shared" not in sources
    listed = " ".join(imported.skipped_sources)
    assert "mail:home" in listed
    assert "draft_folder_id" in listed
    assert "mail:old_com" in listed
    assert "calendar:shared" in listed


# --- connection profile and secrets ---------------------------------------------------


def test_s17_g_icloud_is_a_disabled_caldav_placeholder_with_and_1_related_checks(
    data_root: Path, imported: ImportReport, secrets: MemorySecrets
) -> None:
    # g_icloud_is_a_disabled_caldav_placeholder_with_its_credential_name
    icloud = load_connections(data_root).sources["calendar:icloud"].model_dump()
    assert icloud["kind"] == "caldav"
    assert icloud["enabled"] is False
    assert icloud["username"] == "owner@icloud.example"
    assert icloud["credential"] == "password:owner@icloud.example"

    # s17_secrets_move_to_the_store_and_never_into_a_file
    stored = " ".join(secrets.items.values())
    for value in ("client-secret-value", "refresh-token-value", "abcd-efgh-ijkl-mnop"):
        assert value in stored
    for path in data_root.rglob("*"):
        if path.is_file() and path.suffix != ".sqlite3":
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not any(
                v in text for v in ("client-secret-value", "refresh-token-value", "abcd-efgh")
            )
    assert imported.secrets["icloud app password"] == "copied"


def test_s17_an_existing_different_secret_is_never_overwritten(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    secrets.set("chamberlain.caldav", "password:owner@icloud.example", "a-newer-password")
    report = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert report.secrets["icloud app password"] == "differs; kept the existing one"
    assert secrets.get("chamberlain.caldav", "password:owner@icloud.example") == "a-newer-password"


def test_g_an_existing_profile_is_kept(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    profile = data_root / "config" / "connections.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("version: 1\nsources: {}\n", encoding="utf-8")
    report = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert profile.read_text(encoding="utf-8") == "version: 1\nsources: {}\n"
    assert report.config_written.startswith("kept existing")


# --- cards, actions, evidence ---------------------------------------------------------


def test_m13_m_open_cards_import_with_their_review_clock_and_3_related_checks(
    data_root: Path, imported: ImportReport, clock: ManualClock
) -> None:
    # m_open_cards_import_with_their_review_clock_and_open_actions
    cards = {r["title"]: r for r in rows(data_root, "SELECT * FROM cards")}
    alpha = cards["Alpha supplier contract"]
    assert alpha["state"] == "open"
    assert alpha["goal"] == "Get the contract signed"
    assert alpha["position"] == "Waiting for the supplier to countersign."
    assert alpha["review_at"] == "2026-01-08T09:00:00Z"  # reviewed 1 Jan + 7 days
    assert alpha["over_cap"] == 0
    actions = rows(
        data_root, "SELECT * FROM actions WHERE card = ? ORDER BY title", alpha["handle"]
    )
    assert [a["title"] for a in actions] == ["Step act-1", "Step act-2"]
    assert actions[0]["due_at"] == "2026-01-06T09:00:00Z"
    assert actions[1]["snooze_until"] == "2026-01-10T09:00:00Z"

    # m13_a_card_over_a_cap_imports_as_is_flagged_and_due
    big = next(r for r in rows(data_root, "SELECT * FROM cards") if r["title"] == "Oversized card")
    assert big["over_cap"] == 1
    assert len(big["position"]) == 2000  # stored as it is, nothing cut
    assert big["review_at"] == "2026-01-05T09:00:00Z"  # due at once
    assert any("case-big" in line for line in imported.over_cap_cards)
    long_action = rows(data_root, "SELECT title FROM actions WHERE card = ?", big["handle"])[0]
    assert len(long_action["title"]) == 160  # the one field a hard limit forces to be cut
    assert any("act-long" in w for w in imported.warnings)

    # m_a_closed_card_keeps_one_outcome_line
    done = next(
        r for r in rows(data_root, "SELECT * FROM cards") if r["title"] == "Finished matter"
    )
    assert done["state"] == "closed"
    assert done["outcome"] == "Resolved by phone on the 2nd."
    assert (done["goal"], done["position"], done["rules"]) == ("", "", "")

    # m_evidence_comes_across_only_when_referenced_and_at_most_15_per_card
    texts = [r["text"] for r in rows(data_root, "SELECT text FROM evidence")]
    assert not any("Nobody refers to me" in t for t in texts)
    on_alpha = rows(
        data_root,
        "SELECT count(*) AS n FROM evidence_refs r JOIN cards c ON c.handle = r.owner "
        "WHERE c.title = 'Alpha supplier contract'",
    )[0]["n"]
    assert on_alpha == 15
    assert imported.left_behind["evidence beyond 15 per open card"] == 5


def test_s14_g6_g5_handles_are_valid_and_deterministic_and_3_related_checks(
    data_root: Path, imported: ImportReport
) -> None:
    # s14_handles_are_valid_and_deterministic
    for table in ("cards", "actions", "grants", "operations", "evidence"):
        for row in rows(data_root, f"SELECT handle FROM {table}"):
            prefix, _, token = row["handle"].partition("_")
            assert prefix in {"card", "act", "grant", "op", "evd"}
            assert len(token) == 16

    # g6_a_standing_grant_imports_suspended_and_due_for_review_at_once
    dated = grants(data_root)["Standing sa-dated"]
    assert dated["state"] == "suspended"
    assert dated["kind"] == "standing"
    assert dated["requirement"] == "human_confirmation"
    assert dated["confirmed_via"] == "import"
    assert dated["scope_json"] is None  # the old scope was free text; nothing is authorised
    assert dated["granted_at"] == "2025-12-15T09:00:00Z"  # the original date, kept
    assert dated["review_at"] <= "2026-01-05T09:00:00Z"
    assert len(imported.standing_grants) == 3

    # g6_a_standing_grant_with_no_recorded_date_is_due_and_undated
    undated = grants(data_root)["Standing sa-undated"]
    assert undated["granted_at"] is None
    assert undated["state"] == "suspended"
    assert undated["review_at"] == "2026-01-05T09:00:00Z"

    # g5_an_expired_standing_grant_imports_expired_not_active
    expired = grants(data_root)["Standing sa-expired"]
    assert expired["state"] == "expired"
    assert expired["ended_at"] == "2025-12-31T09:00:00Z"


# --- grants ---------------------------------------------------------------------------


def grants(data_root: Path) -> dict[str, sqlite3.Row]:
    return {r["title"]: r for r in rows(data_root, "SELECT * FROM grants")}


def test_g15_g5_no_recipient_becomes_known_by_import_and_3_related_checks(
    data_root: Path, imported: ImportReport
) -> None:
    # g15_no_recipient_becomes_known_by_import
    assert rows(data_root, "SELECT * FROM known_recipients") == []

    # g5_one_off_grants_follow_the_seven_day_rule_and_their_journals
    by_subject = {
        r["title"]: r for r in rows(data_root, "SELECT * FROM grants WHERE kind = 'one_off'")
    }
    assert by_subject["Subject oa-fresh"]["state"] == "active"
    assert by_subject["Subject oa-fresh"]["expires_at"] == "2026-01-10T10:00:00Z"
    assert by_subject["Subject oa-old"]["state"] == "expired"
    assert by_subject["Subject oa-sent"]["state"] == "spent"  # its journal says it was sent
    assert "Subject oa-revoked" not in by_subject
    for row in by_subject.values():
        assert row["requirement"] == "human_confirmation"
        assert row["scope_json"] is None

    # g_the_old_hash_is_kept_inert_not_recomputed
    assert grants(data_root)["Subject oa-fresh"]["content_sha256"] == "a" * 64

    # g_the_old_wording_of_a_standing_grant_is_kept_as_evidence
    handle = grants(data_root)["Standing sa-dated"]["handle"]
    texts = [
        r["text"]
        for r in rows(
            data_root,
            "SELECT e.text FROM evidence e JOIN evidence_refs r ON r.evidence = e.handle "
            "WHERE r.owner = ?",
            handle,
        )
    ]
    assert any("forward invoices" in t and "accountant only" in t for t in texts)


# --- operations -----------------------------------------------------------------------


def operations(data_root: Path) -> dict[str, sqlite3.Row]:
    return {r["idempotency_key"]: r for r in rows(data_root, "SELECT * FROM operations")}


def test_w_an_in_flight_send_is_left_with_the_old_harness_and_3_related_checks(
    data_root: Path, imported: ImportReport
) -> None:
    # w_an_in_flight_send_is_closed_as_left_with_the_old_harness_and_cannot_be_dispatched
    op = operations(data_root)["import:thunderbird:send:oa-inflight"]
    assert op["state"] == "abandoned"
    assert json.loads(op["receipt_json"])["summary"]["state"] == "abandoned"
    assert len(imported.left_with_old_harness) == 5
    assert op["kind"] == "mail.send"
    assert op["source_id"] == "mail:work"
    assert op["prepared_json"] is None  # nothing to re-dispatch
    assert op["intent_json"] is None
    assert op["lease_owner"] is None
    grant = rows(data_root, "SELECT handle FROM grants WHERE title = 'Subject oa-inflight'")[0]
    assert op["grant"] == grant["handle"]  # the grant is held by it (G-10)

    # w_any_journal_not_in_a_final_state_is_abandoned_and_a_calendar_one_lands_on_its_source
    for key in [
        "import:outlook-send:oa-com",
        "import:native-send:oa-native",  # `draft-saved` is not final
        "import:thunderbird:trash:t-1",  # a stage this code has never seen
        "import:thunderbird:calendar-create:c-1",
    ]:
        assert operations(data_root)[key]["state"] == "abandoned", key
    assert (
        operations(data_root)["import:thunderbird:calendar-create:c-1"]["source_id"]
        == "calendar:work"
    )

    # w_finished_journals_become_receipts_and_clean_ones_are_released
    done = operations(data_root)["import:thunderbird:send:oa-sent"]
    assert done["state"] == "confirmed"
    receipt = json.loads(done["receipt_json"])
    assert receipt["summary"]["state"] == "confirmed"
    assert operations(data_root)["import:thunderbird:move:m-1"]["state"] == "failed_clean"

    # w_an_imported_operation_is_never_in_a_retryable_state
    for op in operations(data_root).values():
        assert op["state"] in {"abandoned", "confirmed", "failed_clean"}
        assert op["state"] not in {"proposed", "authorised", "prepared"}


def test_w_the_tick_after_an_import_neither_fails_nor_and_1_related_checks(
    data_root: Path, imported: ImportReport, secrets: MemorySecrets, clock: ManualClock
) -> None:
    # w_the_tick_after_an_import_neither_fails_nor_changes_an_uncertain_operation
    before = {k: v["state"] for k, v in operations(data_root).items()}
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        for _ in range(3):
            clock.advance(minutes=20)
            app.call("doctor", {})
        events = {r["kind"] for r in app.context.db.execute("SELECT kind FROM events")}
    finally:
        app.close()
    assert {k: v["state"] for k, v in operations(data_root).items()} == before
    assert "operation.uncertain" not in events  # nothing about an old journal is ever work
    assert "grant.review_due" in events


def test_g7_w_the_agent_cannot_reconcile_an_imported_and_1_related_checks(
    data_root: Path, imported: ImportReport, secrets: MemorySecrets, clock: ManualClock
) -> None:
    # w_the_agent_cannot_reconcile_an_imported_operation_into_a_resend
    handle = operations(data_root)["import:thunderbird:send:oa-inflight"]["handle"]
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        started = app.call("start", {"agent": "test"})
        assert started.ok
        reply = app.call("reconcile", {"operation": handle, "session": started.data.session})  # type: ignore[union-attr]
        assert reply.ok  # already closed: nothing to read, nothing to resend
    finally:
        app.close()
    assert operations(data_root)["import:thunderbird:send:oa-inflight"]["state"] == "abandoned"

    # g7_keeping_an_undated_imported_grant_works_and_activates_nothing_new
    handle = grants(data_root)["Standing sa-undated"]["handle"]
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        kept = keep_grant(app.context, handle)
    finally:
        app.close()
    assert kept.scope is None  # still authorises nothing: it has no scope


# --- audit and what is left for the owner ---------------------------------------------


def test_g20_s14_the_import_is_audited_and_the_chain_verifies_and_3_related_checks(
    data_root: Path, imported: ImportReport, secrets: MemorySecrets, clock: ManualClock
) -> None:
    # g20_the_import_is_audited_and_the_chain_verifies
    kinds = {r["kind"]: r for r in rows(data_root, "SELECT kind, actor FROM audit_log")}
    assert {"import", "grant.imported", "operation.imported"} <= set(kinds)
    assert {r["actor"] for r in rows(data_root, "SELECT DISTINCT actor FROM audit_log")} == {
        "system:import"
    }
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        reply = app.call("doctor", {"check": "audit"})
        assert reply.ok
    finally:
        app.close()

    # s14_private_guidance_goes_to_the_private_owner_file_not_the_store
    assert set(imported.private_guidance) == {"filing", "reporting"}
    stored = json.dumps(dump_store(data_root), default=str)
    assert "Private filing rule" not in stored
    report = json.loads((data_root / "import" / "report.json").read_text(encoding="utf-8"))
    assert report["owner_instructions_added"] == ["filing", "reporting"]
    assert "Private filing rule" not in json.dumps(report)
    owner_file = (data_root / "config" / "owner-instructions.yaml").read_text(encoding="utf-8")
    assert "Private filing rule: file receipts under Finance." in owner_file

    # s14_the_printed_summary_names_topics_but_carries_no_private_text
    printed = json.dumps(imported.summary())
    assert "filing" in printed
    assert "Private filing rule" not in printed
    assert "refresh-token-value" not in printed

    # s14_runs_and_review_history_are_left_behind_and_counted
    assert imported.left_behind["runs"] == 1
    assert imported.left_behind["message_reviews"] == 1
    assert imported.unmapped_settings["schedule"] == {"calendar_days_ahead": 3}
    assert imported.unmapped_settings["updates"] == {"interval_hours": 24}


def test_g15_g_old_schedule_and_update_settings_become_and_3_related_checks(
    data_root: Path,
    imported: ImportReport,
    old_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
) -> None:
    # g_old_schedule_and_update_settings_become_connection_settings
    connections = load_connections(data_root)
    assert [(s.name, s.kind, s.at, s.zone, s.weekdays) for s in connections.schedules] == [
        ("morning", "mail", "08:00", None, (1, 2, 3, 4, 5, 6, 7)),
        ("continuation-1400", "mail", "14:00", None, (1, 2, 3, 4, 5, 6, 7)),
        ("continuation-1900", "mail", "19:00", None, (1, 2, 3, 4, 5, 6, 7)),
    ]
    assert connections.updates.enabled is True
    assert connections.updates.remote == "upstream"

    # w_an_imported_operation_keeps_a_summary_but_never_an_intent
    mail = operations(data_root)["import:thunderbird:send:oa-inflight"]
    assert json.loads(mail["summary_json"]) == {
        "title": "Inflight subject",
        "to": ["a@example.test"],
        "cc": ["b@example.test"],
    }
    assert "Never kept" not in str(dict(mail))  # the body is not stored anywhere on the row
    calendar = operations(data_root)["import:thunderbird:calendar-create:c-1"]
    assert json.loads(calendar["summary_json"]) == {
        "title": "Dentist",
        "start": "2026-03-01T10:00:00Z",
        "calendar": "calendar:work",
        "to": [],
        "cc": [],
    }
    for op in (mail, calendar):
        assert op["intent_json"] is None
        assert op["prepared_json"] is None

    # w_running_the_import_again_adds_summaries_to_operations_that_lack_them
    key = "import:thunderbird:send:oa-inflight"
    db = connect(data_root / "chamberlain.sqlite3")
    try:
        db.execute("UPDATE operations SET summary_json = NULL, attempted_at = NULL")
        db.commit()
    finally:
        db.close()
    again = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert again.counts.get("operation_summaries", 0) >= 2
    assert operations(data_root)[key]["summary_json"] is not None
    before = dump_store(data_root)
    third = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert not third.counts
    assert dump_store(data_root) == before

    # g15_the_import_seeds_known_recipients_from_sent_history_and_reports_counts
    from chamberlain.connectors.fake import FakeProvider

    fake = FakeProvider(clock)
    fake.mail.add_source("mail:seeded")
    fake.mail.add_message(
        "mail:seeded",
        folder_id="f-sent",
        internet_message_id="<s1@example.org>",
        sender="me@example.org",
        to=("Friend@Example.org",),
        cc=("boss@example.org",),
    )
    report = run_import(
        old_root, data_root, clock=clock, secrets=secrets, mail={"mail:seeded": fake.mail}
    )
    assert report.recipients["mail:seeded"]["added"] == 2
    assert report.recipients["mail:seeded"]["complete"] is True
    stored = rows(data_root, "SELECT address, origin, grant FROM known_recipients ORDER BY 1")
    assert [(r["address"], r["origin"], r["grant"]) for r in stored] == [
        ("boss@example.org", "sent-history", None),
        ("friend@example.org", "sent-history", None),
    ]
    assert rows(data_root, "SELECT * FROM grants WHERE title LIKE '%sent-history%'") == []


# --- what an old journal said, kept for display and matching only ------------------------


def test_s17_the_old_windows_icloud_password_moves_and_its_calendars_connect(
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrading on Windows: the DPAPI file is read, checked, stored and every calendar added."""
    import base64

    from chamberlain.connectors.caldav.setup import Discovered, adopt_old_credential
    from chamberlain.importer import dpapi

    (old_root / "icloud-app-password.json").unlink()
    (old_root / "icloud-caldav.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "appleAccount": "owner@icloud.example",
                "protectionScope": "LocalMachine",
                "encryptedPassword": base64.b64encode(b"sealed").decode(),
            }
        ),
        encoding="utf-8",
    )

    def unseal(data: bytes, entropy: bytes | None) -> bytes:
        assert data == b"sealed"
        assert entropy == b"OutlookAutomation-iCloud-CalDAV-v2"
        return b"wxyz-wxyz-wxyz-wxyz"

    monkeypatch.setattr(dpapi, "unprotect", unseal)
    signed_in: list[tuple[str, str]] = []

    def icloud(username: str, password: str) -> list[Discovered]:
        signed_in.append((username, password))
        return [
            Discovered("Family", "https://caldav.example/1/family/"),
            Discovered("Home", "https://caldav.example/1/home/"),
        ]

    report = run_import(old_root, data_root, clock=clock, secrets=secrets, caldav_check=icloud)
    assert signed_in == [("owner@icloud.example", "wxyz-wxyz-wxyz-wxyz")]
    assert secrets.get("chamberlain.caldav", "password:owner@icloud.example") == (
        "wxyz-wxyz-wxyz-wxyz"
    )
    sources = load_connections(data_root).sources
    home, family = sources["calendar:icloud"], sources["calendar:icloud-family"]
    assert home.enabled
    assert family.enabled
    assert home.model_dump()["url"] == "https://caldav.example/1/home/"
    assert "calendar:icloud-family" in report.sources
    for path in data_root.rglob("*"):
        if path.is_file() and path.suffix != ".sqlite3":
            assert "wxyz-wxyz" not in path.read_text(encoding="utf-8", errors="ignore")

    # A user who imported before this existed runs one step; it is idempotent.
    again = adopt_old_credential(data_root, old_root, secrets, check=icloud)
    assert again[0] == "owner@icloud.example"
    assert [sid for sid, _ in again[1]] == ["calendar:icloud", "calendar:icloud-family"]
