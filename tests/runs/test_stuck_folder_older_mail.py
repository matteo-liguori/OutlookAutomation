"""Mail that sat more than a day in a stuck folder raises the older-mail question (S8, A-27)."""

import json

from runs_rig import MAIL, Rig

from chamberlain.app.wiring import run_tick

ARCHIVE = "f-archive"


def question(rig: Rig) -> list[dict[str, object]]:
    rows = rig.rows(
        "SELECT attrs_json FROM events WHERE kind = 'source.stale' "
        "AND state IN ('new', 'delivered')"
    )
    return [json.loads(r["attrs_json"]) for r in rows if "older_mail" in r["attrs_json"]]


def test_mail_stuck_for_more_than_a_day_is_asked_about_not_skipped_silently(rig: Rig) -> None:
    run_tick(rig.app)
    rig.fake.mail.stick_folder(MAIL, ARCHIVE)
    rig.clock.advance(minutes=30)
    rig.fake.mail.add_message(
        MAIL,
        folder_id=ARCHIVE,
        internet_message_id="<lost@x>",
        date=rig.clock.now(),
        unsynced=True,
    )
    for _ in range(11):  # about a day and a half with the folder stuck
        rig.clock.advance(hours=3)
        run_tick(rig.app)
    assert question(rig) == []

    rig.fake.mail.stick_folder(MAIL, ARCHIVE, stuck=False)
    rig.clock.advance(minutes=20)
    run_tick(rig.app)
    run_tick(rig.app)
    keys = {r["message_key"] for r in rig.rows("SELECT message_key FROM mail_seen")}
    assert "lost@x" not in keys, "older than a day: not read without the owner's yes"
    asked = question(rig)
    assert [a["reason"] for a in asked] == ["older_mail"]


def test_only_a_copy_in_a_sent_folder_teaches_a_recipient_never_a_from_line(rig: Rig) -> None:
    """S2: mail that merely claims to be from the owner never makes its recipients known."""
    rig.ctx.db.execute(
        "INSERT INTO sources (id, kind, connector, binding_json, created_at, updated_at) "
        "VALUES (?, 'mail', 'fake', ?, '2026-01-05T06:00:00Z', '2026-01-05T06:00:00Z')",
        (MAIL, json.dumps({"address": "owner@example.org"})),
    )
    run_tick(rig.app)
    rig.clock.advance(minutes=10)
    for folder, message_id, recipient in (
        ("f-archive", "<forged@x>", "attacker@elsewhere.example"),
        ("f-sent", "<real@x>", "friend@elsewhere.example"),
    ):
        rig.fake.mail.add_message(
            MAIL,
            folder_id=folder,
            internet_message_id=message_id,
            sender="owner@example.org",
            to=(recipient,),
            date=rig.clock.now(),
        )
    rig.clock.advance(minutes=10)
    run_tick(rig.app)
    known = {r["address"] for r in rig.rows("SELECT address FROM known_recipients")}
    assert known == {"friend@elsewhere.example"}
