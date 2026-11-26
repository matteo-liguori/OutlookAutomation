"""Known recipients: own addresses and Sent-history seeding (G-15, owner decision 2026-11-24)."""

from datetime import timedelta

from chamberlain.connectors.fake import Fault
from chamberlain.writes import repo
from chamberlain.writes.seed import seed_source
from support.write_rig import MAIL, WriteRig


def bind_mailbox(rig: WriteRig, mailbox: str) -> None:
    rig.db.execute(
        "INSERT INTO sources (id, kind, connector, label, binding_json, state, created_at, "
        "updated_at) VALUES (?, 'mail', 'thunderbird', '', ?, 'enabled', 'x', 'x')",
        (MAIL, f'{{"kind": "thunderbird", "mailbox": "{mailbox}"}}'),
    )


def sent(rig: WriteRig, number: int, **kwargs: object) -> None:
    rig.fake.mail.add_message(
        MAIL,
        folder_id="f-sent",
        internet_message_id=f"<sent{number}@example.org>",
        sender="owner@example.org",
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_sources_own_address_is_always_a_known_recipient(rig: WriteRig) -> None:
    bind_mailbox(rig, "Me@Example.org")
    assert "me@example.org" in repo.known_recipients(rig.db, MAIL)
    proposal = rig.engine().propose_intent(rig.send(to=("me@example.org",)))
    assert proposal.requirement.value == "agent_evidence"
    other = rig.engine().propose_intent(rig.send(to=("stranger@example.org",), subject="Other"))
    assert other.requirement.value == "human_confirmation"


def test_seeding_adds_to_and_cc_of_sent_mail_with_the_sent_history_origin(rig: WriteRig) -> None:
    sent(rig, 1, to=("Friend@example.org",), cc=("boss@example.org",))
    sent(rig, 2, to=("friend@example.org",))
    rig.fake.mail.add_message(
        MAIL, internet_message_id="<in1@example.org>", sender="spam@example.net"
    )
    report = seed_source(rig.db, rig.fake.mail, rig.clock, MAIL, months=24)
    assert report.complete
    assert report.scanned == 2
    assert report.added == 2
    assert repo.known_recipients(rig.db, MAIL) == {"friend@example.org", "boss@example.org"}
    origins = {r["address"]: r["origin"] for r in rig.db.execute("SELECT * FROM known_recipients")}
    assert set(origins.values()) == {"sent-history"}
    assert "spam@example.net" not in origins
    again = seed_source(rig.db, rig.fake.mail, rig.clock, MAIL, months=24)
    assert again.added == 0
    assert again.known == 2


def test_seeding_never_downgrades_a_confirmed_recipient_and_reads_no_further_back(
    rig: WriteRig,
) -> None:
    rig.know("friend@example.org")
    sent(rig, 1, to=("friend@example.org",))
    sent(rig, 2, to=("ancient@example.org",), date=rig.clock.now() - timedelta(days=800))
    report = seed_source(rig.db, rig.fake.mail, rig.clock, MAIL, months=24)
    assert report.added == 0
    assert "ancient@example.org" not in repo.known_recipients(rig.db, MAIL)
    row = rig.db.execute("SELECT origin FROM known_recipients").fetchone()
    assert row["origin"] == "confirmed-grant"


def test_seeding_pages_to_completion(rig: WriteRig) -> None:
    for number in range(450):
        sent(rig, number, to=(f"person{number}@example.org",))
    report = seed_source(rig.db, rig.fake.mail, rig.clock, MAIL, months=24)
    assert report.complete
    assert report.scanned == 450
    assert report.added == 450


def test_a_partial_scan_is_reported_incomplete_but_keeps_what_it_saw(rig: WriteRig) -> None:
    sent(rig, 1, to=("friend@example.org",))
    rig.fake.world.arm(Fault.STALE)
    report = seed_source(rig.db, rig.fake.mail, rig.clock, MAIL, months=24)
    assert not report.complete
    assert report.reason
    assert report.added == 1


def test_a_source_that_cannot_be_read_is_reported_not_raised(rig: WriteRig) -> None:
    report = seed_source(rig.db, rig.fake.mail, rig.clock, "mail:missing", months=24)
    assert not report.complete
    assert report.added == 0
    assert report.reason


def test_doctor_shows_the_known_recipient_counts_per_source(rig: WriteRig) -> None:
    from chamberlain.writes.handlers import recipient_checks

    sent(rig, 1, to=("friend@example.org",))
    [before] = recipient_checks(rig.ctx)
    assert before.status == "warn"
    assert "0 from Sent history" in before.summary
    seed_source(rig.db, rig.fake.mail, rig.clock, MAIL, months=24)
    [after] = recipient_checks(rig.ctx)
    assert after.status == "ok"
    assert "1 from Sent history" in after.summary


def test_doctor_check_ids_are_valid_for_any_source_id() -> None:
    """Live-suite bug: `mail:lab-a` gave a check id the doctor schema refused."""
    from chamberlain.schema.tools import CheckResult
    from chamberlain.writes.handlers import check_id

    for source in ("mail:lab-a", "mail:test", "calendar:Work-2"):
        CheckResult(id=check_id("recipients", source), status="ok", summary="s")
