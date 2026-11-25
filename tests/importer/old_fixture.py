# ruff: noqa: E501
"""A synthetic old installation, built from the old schema's DDL (never the owner's data).

The DDL below is copied from the old harness (`monitor/storage.py`, schema 9) so the
fixture has the same tables and columns. Nothing here imports the old harness, whose
start-up takes backups and prunes. All names, addresses and ids are made up.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE cases(id TEXT PRIMARY KEY,title TEXT NOT NULL,position TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE actions(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),status TEXT NOT NULL,next_review TEXT,details TEXT NOT NULL);
CREATE TABLE decisions(id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id),details TEXT NOT NULL);
CREATE TABLE evidence(id TEXT PRIMARY KEY,metadata TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE context(id TEXT PRIMARY KEY,entity TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE approvals(id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id),details TEXT NOT NULL);
CREATE TABLE standing_approvals(id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,details TEXT NOT NULL);
CREATE TABLE mutations(key TEXT PRIMARY KEY,hash TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,status TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE candidates(run_id TEXT REFERENCES runs(id),id TEXT,details TEXT NOT NULL,PRIMARY KEY(run_id,id));
CREATE TABLE message_reviews(id TEXT PRIMARY KEY,source TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE message_versions(id TEXT PRIMARY KEY,details TEXT NOT NULL);
"""


class MemorySecrets:
    """A `SecretStore` kept in a dict (a test never touches the real credential store)."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get(self, service: str, name: str) -> str | None:
        return self.items.get((service, name))

    def set(self, service: str, name: str, value: str) -> None:
        self.items[(service, name)] = value

    def delete(self, service: str, name: str) -> bool:
        return self.items.pop((service, name), None) is not None


BIG_CONTEXT = "c" * 6000

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64

OWNER = "owner@example.test"
HOME = "home@example.test"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "Test Owner",
        "timezone": "Europe/London",
        "mail": {"mail:work": OWNER, "mail:home": HOME, "mail:old_com": "legacy@example.test"},
        "calendars": ["calendar:work", "calendar:primary", "calendar:icloud", "calendar:shared"],
        "calendar_providers": {"calendar:shared": "google"},
        "permission_defaults": {"sending": "ask-every-time", "routine_filing": True},
        "instructions": {"filing": "Private filing rule: file receipts under Finance."},
        "platform": {
            "os": "windows",
            "outlook_backend": "thunderbird",
            "bindings": {
                "mail:work": {
                    "account_id": "account1",
                    "selected_inbox_id": "account1:/INBOX",
                    "draft_folder_id": "account1:/Drafts",
                    "sent_folder_id": "account1:/Sent",
                    "trash_folder_id": "account1:/Trash",
                    "address_book_ids": ["book1"],
                },
                "mail:home": {"account_id": "account2", "selected_inbox_id": "account2:/INBOX"},
                "mail:old_com": {"backend": "com"},
                "calendar:work": {"calendar_id": "owl-cal-1"},
                "calendar:primary": {
                    "transport": "google",
                    "calendar_id": "owner@gmail.example/primary",
                },
            },
        },
    }


OLD_REPORT_TEMPLATE = """# Pass summary DD/MM/YYYY

Write briefly. Use {{timezone}} times.

**🟥 ACCESS PROBLEM - CHECK NEEDED**

Source; what is wrong; last good time.

**🟧 IMPORTANT APPOINTMENT - PLEASE ACKNOWLEDGE**

Title; start; place.

## Open items

List what changed and the next step.
Include the {{schedule.roundup_day}} roundup when due.

## Permission reviews

Say which are due. In the written report mention calendar items only when action is needed.

## Verification

Per source results.
"""


def customisations() -> dict[str, Any]:
    return {
        "schedule": {"morning": "08:00", "continuations": [840, 1140], "calendar_days_ahead": 3},
        "updates": {"enabled": True, "remote": "upstream", "interval_hours": 24},
        "instructions": {"reporting": "Private reporting rule: lead with calendar clashes."},
        "templates": {"report": OLD_REPORT_TEMPLATE, "morning": "Start the morning pass."},
    }


def _case(
    cursor: sqlite3.Cursor, cid: str, title: str, position: str, status: str, **extra: Any
) -> None:
    details = {"purpose": extra.pop("purpose", title), "context": "", **extra}
    cursor.execute(
        "INSERT INTO cases VALUES(?,?,?,?,?,?,?)",
        (
            cid,
            title,
            position,
            status,
            3,
            extra.get("updated", "2026-01-02T10:00:00+00:00"),
            dumps(details),
        ),
    )


def _action(
    cursor: sqlite3.Cursor, aid: str, cid: str, status: str, due: str | None, **extra: Any
) -> None:
    details = {"id": aid, "status": status, "next_review": due, "next_step": f"Step {aid}", **extra}
    cursor.execute("INSERT INTO actions VALUES(?,?,?,?,?)", (aid, cid, status, due, dumps(details)))


def _evidence(cursor: sqlite3.Cursor, eid: str, label: str, body: str, at: str) -> None:
    cursor.execute(
        "INSERT INTO evidence VALUES(?,?,?)",
        (eid, dumps({"label": label, "captured_at": at}), body),
    )


def build_old_root(root: Path) -> Path:
    """Write a complete old installation under `root` and return it."""
    (root / "monitor").mkdir(parents=True)
    (root / "monitor" / "profile.json").write_text(json.dumps(profile()), encoding="utf-8")
    (root / "customisations.yaml").write_text(yaml.safe_dump(customisations()), encoding="utf-8")

    db = sqlite3.connect(root / "monitor" / "monitor.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.execute("PRAGMA user_version=9")
    cur = db.cursor()
    cur.execute("INSERT INTO meta VALUES('mode','active')")

    _case(
        cur,
        "case-alpha",
        "Alpha supplier contract",
        "Waiting for the supplier to countersign.",
        "open",
        purpose="Get the contract signed",
        review_interval_days=7,
        card_reviewed_at="2026-01-01T09:00:00+00:00",
        evidence_index=[
            {"id": f"ev-{n}", "at": f"2026-01-0{n % 9 + 1}T08:00:00+00:00"} for n in range(1, 21)
        ],
    )
    _case(
        cur,
        "case-big",
        "Oversized card",
        "x" * 2000,
        "open",
        card_reviewed_at="2026-01-04T09:00:00+00:00",
        context=BIG_CONTEXT,
    )
    _case(
        cur,
        "case-done",
        "Finished matter",
        "Resolved by phone on the 2nd.",
        "closed",
        context="History nobody needs now.",
    )
    _action(cur, "act-1", "case-alpha", "needs-user", "2026-01-06T09:00:00+00:00")
    _action(
        cur,
        "act-2",
        "case-alpha",
        "waiting",
        "2026-01-12T09:00:00+00:00",
        snooze_until="2026-01-10T09:00:00+00:00",
    )
    _action(cur, "act-long", "case-big", "watch", None, next_step="L" * 300)
    for n in range(1, 21):
        _evidence(
            cur,
            f"ev-{n}",
            f"Source {n}",
            f"Statement number {n}.",
            f"2026-01-0{n % 9 + 1}T08:00:00+00:00",
        )
    _evidence(
        cur,
        "ev-grant",
        "User approval",
        "The owner said yes to the Alpha send.",
        "2026-01-03T09:00:00+00:00",
    )
    _evidence(cur, "ev-unused", "Nobody refers to me", "Left behind.", "2025-12-01T09:00:00+00:00")

    message = {
        "sender": OWNER,
        "to": ["supplier@example.test"],
        "cc": [],
        "bcc": [],
        "subject": "Signed contract",
        "body": "Please find the contract attached.",
        "attachments": [],
    }

    def one_off(aid: str, granted: str | None, status: str, digest: str) -> None:
        details = {
            "id": aid,
            "kind": "one-off",
            "status": status,
            "message": {**message, "subject": f"Subject {aid}"},
            "message_hash": digest,
            "granted_at": granted,
            "recorded_at": granted,
            "evidence_ids": ["ev-grant"],
        }
        cur.execute("INSERT INTO approvals VALUES(?,?,?)", (aid, "case-alpha", dumps(details)))

    one_off("oa-fresh", "2026-01-03T10:00:00+00:00", "approved", HASH_A)
    one_off("oa-old", "2025-11-01T10:00:00+00:00", "approved", HASH_B)
    one_off("oa-sent", "2026-01-04T10:00:00+00:00", "approved", HASH_C)
    one_off("oa-inflight", "2026-01-04T11:00:00+00:00", "approved", HASH_D)
    one_off("oa-revoked", "2026-01-04T12:00:00+00:00", "revoked", HASH_E)

    def standing(aid: str, granted: str | None, expires: str | None = None) -> None:
        details = {
            "id": aid,
            "kind": "standing",
            "status": "approved",
            "title": f"Standing {aid}",
            "summary": "Forward invoices to the accountant.",
            "scope": {"operation": "forward invoices", "mailbox": OWNER},
            "triggers": "an invoice arrives from a known supplier",
            "boundaries": "accountant only",
            "scope_hash": HASH_A,
            "granted_at": granted,
            "expires_at": expires,
            "next_review_at": "2026-02-01T09:00:00+00:00",
            "evidence_ids": ["ev-grant"],
        }
        cur.execute("INSERT INTO standing_approvals VALUES(?,?,?)", (aid, None, dumps(details)))

    standing("sa-dated", "2025-12-15T09:00:00+00:00")
    standing("sa-undated", None)
    standing("sa-expired", "2025-06-01T09:00:00+00:00", "2025-12-31T09:00:00+00:00")

    def journal(key: str, state: dict[str, Any]) -> None:
        cur.execute("INSERT INTO meta VALUES(?,?)", (key, json.dumps(state)))

    journal(
        "thunderbird:send:oa-sent",
        {"stage": "complete", "fingerprint": HASH_C, "result": {"ok": 1}},
    )
    journal(
        "thunderbird:send:oa-inflight",
        {
            "stage": "attempting",
            "fingerprint": HASH_D,
            "request": {
                "mailbox": OWNER,
                "to": ["a@example.test"],
                "cc": ["b@example.test"],
                "subject": "Inflight subject",
                "body": "Never kept.",
            },
        },
    )
    journal("outlook-send:oa-com", {"stage": "attempting"})
    journal("native-send:oa-native", {"stage": "draft-saved", "fingerprint": HASH_E})
    journal("thunderbird:trash:t-1", {"stage": "stage-from-the-future"})
    journal("thunderbird:move:m-1", {"stage": "not-attempted"})
    journal(
        "thunderbird:calendar-create:c-1",
        {
            "stage": "attempting",
            "request": {
                "calendar_id": "owl-cal-1",
                "icalendar": (
                    "BEGIN:VCALENDAR\r\n"
                    "VERSION:2.0\r\n"
                    "PRODID:-//t//EN\r\n"
                    "BEGIN:VEVENT\r\n"
                    "UID:c-1@test\r\n"
                    "DTSTAMP:20260101T000000Z\r\n"
                    "DTSTART:20260301T100000Z\r\n"
                    "DTEND:20260301T110000Z\r\n"
                    "SUMMARY:Dentist\r\n"
                    "END:VEVENT\r\n"
                    "END:VCALENDAR\r\n"
                ),
            },
        },
    )

    cur.execute("INSERT INTO runs VALUES('run-1','2026-01-01T09:00:00+00:00','done','{}')")
    cur.execute("INSERT INTO message_reviews VALUES('mr-1','mail:work','{}')")
    db.commit()
    db.close()

    google = root / "google"
    (google / "tokens").mkdir(parents=True)
    (google / "client.json").write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "client-1.apps.googleusercontent.com",
                    "client_secret": "client-secret-value",
                }
            }
        ),
        encoding="utf-8",
    )
    (google / "tokens" / "owner@gmail.example.json").write_text(
        json.dumps({"refresh_token": "refresh-token-value", "scope": "calendar"}), encoding="utf-8"
    )
    (root / "icloud-app-password.json").write_text(
        json.dumps({"username": "owner@icloud.example", "password": "abcd-efgh-ijkl-mnop"}),
        encoding="utf-8",
    )
    return root


# --- the owner's own knowledge (rules, decisions, standing facts, watches, appointments) ----

DEFAULT_REPORTING = "Use the supplied report template (the old harness's shipped text)."
DEFAULT_OUTLOOK = "Mail and calendar access: `outlook <operation> --input -` takes JSON."


def add_owner_knowledge(root: Path) -> None:
    """Add to an existing synthetic old root what the owner wrote over the years."""
    state = root / ".state"
    state.mkdir(exist_ok=True)
    (state / "defaults.json").write_text(
        json.dumps(
            {
                "baseline": {
                    "instructions": {"outlook": DEFAULT_OUTLOOK, "reporting": DEFAULT_REPORTING}
                }
            }
        ),
        encoding="utf-8",
    )
    custom = yaml.safe_load((root / "customisations.yaml").read_text(encoding="utf-8"))
    custom["instructions"]["outlook"] = DEFAULT_OUTLOOK  # still the shipped text: not the owner's
    custom["permission_defaults"] = {
        "filing_changes_read_state": True,
        "reporting_marks_read": True,
    }
    (root / "customisations.yaml").write_text(yaml.safe_dump(custom), encoding="utf-8")

    db = sqlite3.connect(root / "monitor" / "monitor.db")
    try:
        row = db.execute("SELECT details FROM cases WHERE id='case-alpha'").fetchone()
        details = json.loads(row[0])
        details["customisations"] = {
            "instructions": {"supplier": "Always copy the finance team on supplier mail."},
            "reviews": {"card_days": 3},
        }
        db.execute("UPDATE cases SET details=? WHERE id='case-alpha'", (dumps(details),))
        row = db.execute("SELECT details FROM cases WHERE id='case-big'").fetchone()
        details = json.loads(row[0])
        details["customisations"] = {"instructions": {"style": "Short replies only."}}
        db.execute("UPDATE cases SET details=? WHERE id='case-big'", (dumps(details),))
        for aid, extra in (
            (
                "act-1",
                {
                    "important_appointment": True,
                    "appointment_title": "Dentist",
                    "appointment_start": "2026-03-01T10:00:00+00:00",
                    "explicit_review": "2026-02-28T09:00:00+00:00",
                },
            ),
            (
                "act-2",
                {"important_appointment": True, "appointment_start": "2026-03-02T10:00:00+00:00"},
            ),
        ):
            row = db.execute("SELECT details FROM actions WHERE id=?", (aid,)).fetchone()
            merged = {**json.loads(row[0]), **extra}
            db.execute("UPDATE actions SET details=? WHERE id=?", (dumps(merged), aid))
        db.execute(
            "INSERT INTO cases VALUES(?,?,?,?,?,?,?)",
            (
                "case-passport",
                "Passport renewal",
                "Appointment booked.",
                "open",
                1,
                "2026-01-02T10:00:00+00:00",
                dumps({"purpose": "Passport renewal", "context": ""}),
            ),
        )
        db.execute(
            "INSERT INTO actions VALUES(?,?,?,?,?)",
            (
                "act-new-important-1",
                "case-passport",
                "needs-user",
                "2026-01-20T09:00:00+00:00",
                dumps(
                    {
                        "id": "act-new-important-1",
                        "status": "needs-user",
                        "next_step": "Passport visit",
                        "next_review": "2026-01-20T09:00:00+00:00",
                        "appointment_start": "2026-04-01T08:30:00+00:00",
                    }
                ),
            ),
        )
        db.execute(
            "INSERT INTO decisions VALUES(?,?,?)",
            (
                "dec-1",
                "case-alpha",
                dumps({"id": "dec-1", "text": "Pay by bank transfer, never card."}),
            ),
        )
        db.execute(
            "INSERT INTO context VALUES(?,?,?)",
            (
                "ctx-1",
                "Acme Ltd",
                dumps(
                    {
                        "id": "ctx-1",
                        "entity": "Acme Ltd",
                        "text": "Our main supplier; contact is Sam.",
                    }
                ),
            ),
        )
        db.execute(
            "INSERT INTO context VALUES(?,?,?)",
            (
                "watch-1",
                "keyword-watch",
                dumps(
                    {
                        "id": "watch-1",
                        "kind": "keyword-watch",
                        "terms": ["tenancy", "deposit"],
                        "text": "Tell the owner at once.",
                        "enabled": True,
                    }
                ),
            ),
        )
        db.commit()
    finally:
        db.close()
