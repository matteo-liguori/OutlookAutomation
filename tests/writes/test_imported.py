"""Imported uncertain operations: matched, settled, closed and quieted without the owner.

Owner ruling: the owner is not asked to settle what the harness or the agent can. An imported
operation has no intent, so nothing here can ever send it; a match against the live source or
the agent's evidence settles it as `confirmed`; absence is never concluded without proof.
"""

from datetime import UTC, datetime, timedelta

import pytest

from chamberlain.attention import repo as attention_repo
from chamberlain.connectors.fake import Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.grants import GrantState
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.mail import FolderRole
from chamberlain.domain.operations import OperationState
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import ReconcileRequest
from chamberlain.store.migrate import stamp
from chamberlain.testing.write_rig import CALENDAR, MAIL, SESSION, WriteRig
from chamberlain.writes import repo
from chamberlain.writes.imported import OperationSummary
from chamberlain.writes.maintenance import maintain
from chamberlain.writes.models import OperationRow

START = datetime(2026, 3, 1, 10, tzinfo=UTC)
HANDLE = "op_import0000001"


def imported(
    rig: WriteRig,
    kind: IntentKind,
    summary: OperationSummary | None,
    *,
    source: str,
    attempted: str | None = "2026-02-28T09:00:00Z",
) -> OperationRow:
    """What the importer leaves: uncertain, no intent, no prepared write."""
    now = stamp(rig.clock)
    repo.insert_operation(
        rig.db,
        handle=HANDLE,
        idempotency_key=f"import:{HANDLE}",
        kind=kind,
        source_id=source,
        card=None,
        session=None,
        intent_sha256="d" * 64,
        intent_json="{}",
        refs_json="{}",
        at=now,
    )
    repo.update_operation(
        rig.db,
        HANDLE,
        expected_state=OperationState.PROPOSED,
        state="uncertain",
        intent_json=None,
        refs_json=None,
        dispatched_at=now,
    )
    if summary is not None:
        repo.set_operation_summary(rig.db, HANDLE, summary.model_dump_json(), attempted)
    row = repo.get_operation(rig.db, HANDLE)
    assert row is not None
    return row


def meeting(rig: WriteRig, title: str = "Dentist") -> None:
    rig.fake.calendar.add_event(
        CALENDAR, EventFields(title=title, start=START, end=START + timedelta(hours=1))
    )


def dentist() -> OperationSummary:
    return OperationSummary(title="Dentist", start="2026-03-01T10:00:00Z", calendar=CALENDAR)


def state_of(rig: WriteRig, handle: str = HANDLE) -> OperationState:
    row = repo.get_operation(rig.db, handle)
    assert row is not None
    return row.state


def sent(rig: WriteRig, subject: str, to: str) -> None:
    rig.fake.mail.add_message(
        MAIL,
        folder_id=rig.fake.world.folder_by_role(MAIL, FolderRole.SENT).id,
        internet_message_id=f"<{subject.replace(' ', '')}@example.org>",
        subject=subject,
        to=(to,),
        date=datetime(2026, 2, 28, 9, 5, tzinfo=UTC),
    )


# --- reconcile finds it -----------------------------------------------------------------


def test_an_imported_calendar_write_found_on_the_calendar_settles_confirmed(rig: WriteRig) -> None:
    imported(rig, IntentKind.CALENDAR_CREATE, dentist(), source=CALENDAR)
    meeting(rig)
    after, changed = rig.engine().reconcile(HANDLE)
    assert changed
    assert after.state is OperationState.CONFIRMED
    assert after.intent_json is None  # still not an intent: it can never be sent


def test_a_missing_imported_event_stays_uncertain_and_frees_nothing(rig: WriteRig) -> None:
    imported(rig, IntentKind.CALENDAR_CREATE, dentist(), source=CALENDAR)
    meeting(rig, "Something else")
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().reconcile(HANDLE)
    assert raised.value.code is ErrorCode.OPERATION_UNCERTAIN
    assert state_of(rig) is OperationState.UNCERTAIN


def test_an_imported_send_found_in_sent_settles_confirmed(rig: WriteRig) -> None:
    summary = OperationSummary(title="Invoice 7", to=("pat@example.org",))
    imported(rig, IntentKind.MAIL_SEND, summary, source=MAIL)
    sent(rig, "Re: Invoice 7", "pat@example.org")
    after, _ = rig.engine().reconcile(HANDLE)
    assert after.state is OperationState.CONFIRMED


def test_a_sent_message_to_someone_else_does_not_match(rig: WriteRig) -> None:
    summary = OperationSummary(title="Invoice 7", to=("pat@example.org",))
    imported(rig, IntentKind.MAIL_SEND, summary, source=MAIL)
    sent(rig, "Invoice 7", "someone@example.org")
    with pytest.raises(ChamberlainError):
        rig.engine().reconcile(HANDLE)
    assert state_of(rig) is OperationState.UNCERTAIN


def test_an_imported_delete_is_never_matched_by_reading(rig: WriteRig) -> None:
    imported(rig, IntentKind.CALENDAR_DELETE, dentist(), source=CALENDAR)
    with pytest.raises(ChamberlainError):
        rig.engine().reconcile(HANDLE)
    assert state_of(rig) is OperationState.UNCERTAIN


# --- the agent says it happened ---------------------------------------------------------


def test_the_agent_settles_it_happened_with_its_own_evidence(rig: WriteRig) -> None:
    imported(rig, IntentKind.MAIL_SEND, OperationSummary(title="X", to=("a@b.org",)), source=MAIL)
    evidence = rig.evidence.add(text="Saw it in Sent.", owner=HANDLE, session=SESSION)
    after, changed = rig.engine().reconcile(HANDLE, happened_evidence=evidence)
    assert changed
    assert after.state is OperationState.CONFIRMED


def test_evidence_from_another_session_is_refused(rig: WriteRig) -> None:
    imported(rig, IntentKind.MAIL_SEND, None, source=MAIL)
    evidence = rig.evidence.add(text="Old note.", owner=HANDLE, session="ses_someoneelse1")
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().reconcile(HANDLE, happened_evidence=evidence)
    assert raised.value.code is ErrorCode.REQUEST_INVALID
    assert state_of(rig) is OperationState.UNCERTAIN


def test_the_tool_has_no_way_to_say_it_did_not_happen() -> None:
    with pytest.raises(ValueError, match="Input should be True"):
        ReconcileRequest.model_validate(
            {"operation": HANDLE, "happened": False, "evidence": "evd_abcdefgh1"}
        )
    with pytest.raises(ValueError, match="go together"):
        ReconcileRequest.model_validate({"operation": HANDLE, "happened": True})


def test_an_imported_operation_cannot_be_dispatched(rig: WriteRig) -> None:
    imported(rig, IntentKind.MAIL_SEND, OperationSummary(title="X"), source=MAIL)
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().do(HANDLE)
    assert raised.value.code is ErrorCode.OPERATION_NEEDS_RECONCILE
    assert rig.sent_copies() == 0


# --- closing without freeing --------------------------------------------------------------


def test_an_imported_operation_is_abandoned_after_fourteen_days_freeing_nothing(
    rig: WriteRig,
) -> None:
    imported(rig, IntentKind.CALENDAR_CREATE, dentist(), source=CALENDAR)
    rig.clock.advance(hours=24 * 13)
    maintain(rig.ctx, rig.policy)
    assert state_of(rig) is OperationState.UNCERTAIN
    rig.clock.advance(hours=24 * 2)
    report = maintain(rig.ctx, rig.policy)
    assert report.abandoned == 1
    closed = repo.get_operation(rig.db, HANDLE)
    assert closed is not None
    assert closed.state is OperationState.ABANDONED
    assert closed.receipt_json is not None


def test_a_native_send_is_never_abandoned_and_keeps_its_grant_use(rig: WriteRig) -> None:
    live = rig.ready_send()
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(ChamberlainError):
        rig.engine().do(live.handle)
    grant = rig.engine().get(live.handle).grant or ""
    rig.clock.advance(hours=24 * 20)
    maintain(rig.ctx, rig.policy)
    assert state_of(rig, live.handle) is OperationState.UNCERTAIN  # a send is never quietly closed
    assert repo.live_uses(rig.db, [grant], "x") == {grant: 1}  # still counted as used
    stored = repo.get_grant(rig.db, grant)
    assert stored is not None
    assert stored.state is GrantState.EXPIRED


# --- attention speaks once ---------------------------------------------------------------


def told(rig: WriteRig, handle: str) -> None:
    now = stamp(rig.clock)
    rig.db.execute(
        "INSERT INTO events (handle, kind, subject, dedupe_key, priority, summary, state, "
        "detected_at, delivered_at) VALUES (?, 'operation.uncertain', ?, ?, 95, 's', "
        "'delivered', ?, ?)",
        ("evt_" + handle[-12:], handle, "k:" + handle, now, now),
    )


def raised_now(rig: WriteRig) -> list[str]:
    return [o.handle for o in attention_repo.uncertain_operations(rig.db)]


def test_an_unresendable_operation_is_raised_until_told_then_quiet(rig: WriteRig) -> None:
    imported(rig, IntentKind.CALENDAR_CREATE, dentist(), source=CALENDAR)
    assert raised_now(rig) == [HANDLE]
    told(rig, HANDLE)
    assert raised_now(rig) == []


def test_trying_to_redo_the_same_imported_write_raises_it_again(rig: WriteRig) -> None:
    summary = OperationSummary(title="Invoice 7", to=("pat@example.org",))
    imported(rig, IntentKind.MAIL_SEND, summary, source=MAIL)
    told(rig, HANDLE)
    assert raised_now(rig) == []
    rig.clock.advance(minutes=5)
    rig.know("pat@example.org")
    rig.engine().propose_intent(rig.send(to=("pat@example.org",), subject="Re: Invoice 7"))
    assert raised_now(rig) == [HANDLE]


def test_an_unrelated_new_write_does_not_raise_it_again(rig: WriteRig) -> None:
    imported(rig, IntentKind.MAIL_SEND, OperationSummary(title="Invoice 7"), source=MAIL)
    told(rig, HANDLE)
    rig.clock.advance(minutes=5)
    rig.know("friend@example.org")
    rig.engine().propose_intent(rig.send(subject="Lunch?"))
    assert raised_now(rig) == []
