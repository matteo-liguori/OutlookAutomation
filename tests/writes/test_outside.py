"""Outside actions and several sources in one standing grant (04-writes W-44 to W-47, 05-grants
G-25, G-26): one permission system, nothing verified that the harness did not do itself."""

from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from chamberlain.domain.canonical import canonical_json, content_hash
from chamberlain.domain.grants import ConfirmedVia, GrantState, Requirement, StandingScope
from chamberlain.domain.intents import CalendarCreate, IntentKind, OutsideAction, OutsideCategory
from chamberlain.domain.operations import OperationState
from chamberlain.runs import sections
from chamberlain.runs.models import RunRow, RunState
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import MEMORY, connect
from chamberlain.store.migrate import discover, migrate, stamp
from chamberlain.surface.viewer import pages
from chamberlain.testing.write_rig import CALENDAR, CALENDAR_B, CALENDAR_PLAIN, MAIL, WriteRig
from chamberlain.writes import repo
from chamberlain.writes.confirm import grant_detail, list_grants
from chamberlain.writes.maintenance import maintain
from chamberlain.writes.read_state import propose_missing, scope_for

CARD = "card_0123456789abcdef"
FORM = OutsideCategory.FORM_FILL


def outside(**more: object) -> OutsideAction:
    fields: dict[str, object] = {
        "where": "Council portal",
        "what": "Fill in the bin collection form",
        "enters": "The owner's address",
        "category": FORM,
        "enters_password": False,
        "pays": False,
        "accepts_terms": False,
    }
    return OutsideAction.model_validate({**fields, **more})


def open_card(rig: WriteRig, handle: str = CARD) -> None:
    stored = stamp(rig.clock)
    rig.db.execute(
        "INSERT INTO cards (handle, title, state, created_at, updated_at) "
        "VALUES (?, 'Bins', 'open', ?, ?)",
        (handle, stored, stored),
    )


def standing_outside(rig: WriteRig, **more: object) -> str:
    """The agent asks for a standing permission; the owner clicks once."""
    fields: dict[str, object] = {
        "source": "outside:owner",
        "intent_kinds": frozenset({IntentKind.OUTSIDE_ACTION}),
        "title": "Council forms",
        "summary": "Fill in and send forms on the council portal for the bins card.",
        "where": "council portal",
        "categories": frozenset({FORM, OutsideCategory.FORM_SUBMIT}),
        "card": CARD,
    }
    scope = StandingScope.model_validate({**fields, **more})
    proposal = rig.engine().propose_standing(scope, card=scope.card)
    assert proposal.requirement is Requirement.HUMAN_CONFIRMATION
    assert proposal.grant is not None
    with pytest.raises(ChamberlainError) as refused:  # the agent cannot say yes for the owner
        rig.engine().grants.activate(
            proposal.grant.handle, via=ConfirmedVia.AGENT_EVIDENCE, actor="agent:test"
        )
    assert refused.value.code is ErrorCode.GRANT_HUMAN_CONFIRMATION_REQUIRED
    rig.human_approves(proposal)
    return proposal.grant.handle


def facts_of_pass(rig: WriteRig) -> sections.Facts:
    run = RunRow(
        "run_x", "morning", "2026-01-05", RunState.RUNNING, "2026-01-05T08:00:00Z", None, None, None
    )
    return sections.Facts(rig.ctx, run, ZoneInfo("UTC"))


def test_one_outside_action_by_the_owners_word_is_recorded_never_verified(rig: WriteRig) -> None:
    eng = rig.engine()
    proposal = eng.propose_intent(outside())
    assert proposal.requirement is Requirement.AGENT_EVIDENCE
    assert not proposal.ready
    assert "cannot check it" in proposal.preview
    ready = rig.agent_approves(proposal, "Yes, fill in the bins form.")
    assert ready.ready
    assert ready.operation is not None
    handle = ready.operation.handle

    with pytest.raises(ChamberlainError):  # permission is taken before acting, not after
        eng.do(handle, "Done, reference 123")
    allowed = eng.do(handle)
    assert allowed.state is OperationState.AUTHORISED
    assert eng.do(handle).state is OperationState.AUTHORISED  # asking again changes nothing

    reported = eng.do(handle, "Submitted. Confirmation number BIN-42.")
    assert reported.state is OperationState.AGENT_REPORTED
    assert not (reported.client_verified)
    assert not (reported.server_verified)
    assert eng.do(handle).state is OperationState.AGENT_REPORTED  # a settled one answers the same
    shown = eng.view(reported)
    assert shown.state is OperationState.AGENT_REPORTED
    assert not shown.verification.client_verified
    assert rig.grant_for(reported).state is GrantState.SPENT  # the one-off is used up
    assert rig.mail.reconcile_calls == 0, "no connector was asked anything"
    with pytest.raises(ChamberlainError):  # a report belongs to outside actions only
        eng.do(rig.ready_send().handle, "I sent it")

    text = sections.verification(facts_of_pass(rig))
    assert "Council portal" in text
    assert "BIN-42" in text
    assert "reported by the assistant" in text or "not checked" in text
    assert "every change was verified" not in text, "the report never claims it checked this"

    # Allowed but never reported: closed after a day, and the report says so.
    second = eng.propose_intent(outside(what="Download the bin calendar", category="download"))
    approved = rig.agent_approves(second, "Yes, download it.")
    assert approved.operation is not None
    eng.do(approved.operation.handle)
    rig.clock.advance(hours=25)
    maintain(rig.ctx, rig.policy)
    assert rig.state(approved.operation.handle) == "abandoned"
    assert "never reported" in sections.verification(facts_of_pass(rig))


def test_a_password_a_payment_or_terms_are_refused_whatever_is_granted(rig: WriteRig) -> None:
    open_card(rig)
    standing_outside(rig)
    for flag in ("enters_password", "pays", "accepts_terms"):
        with pytest.raises(ChamberlainError) as refused:
            rig.engine().propose_intent(outside(**{flag: True}), card=CARD)
        assert refused.value.code is ErrorCode.REQUEST_INVALID
        assert "themselves" in (refused.value.fix or "")
    refusals = rig.db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE kind = 'outside.refused'"
    ).fetchone()[0]
    assert refusals == 3, "each refusal is written down"
    assert (
        rig.db.execute("SELECT COUNT(*) FROM operations WHERE kind = 'outside.action'").fetchone()[
            0
        ]
        == 0
    ), "nothing was stored for it"
    with pytest.raises(ValidationError):  # the three flags must always be stated
        OutsideAction.model_validate(
            {"where": "x", "what": "y", "category": "other", "enters_password": False}
        )


def test_a_standing_outside_permission_is_one_click_one_card_and_ends_with_the_card(
    rig: WriteRig,
) -> None:
    open_card(rig)
    other = "card_fedcba9876543210"
    open_card(rig, other)
    with pytest.raises(ValidationError):  # standing use is card-bound only
        StandingScope(
            source="outside:owner",
            intent_kinds=frozenset({IntentKind.OUTSIDE_ACTION}),
            title="x",
            summary="y",
            where="z",
            categories=frozenset({FORM}),
        )
    grant = standing_outside(rig, max_per_day=2)

    ready = rig.engine().propose_intent(outside(), card=CARD)
    assert ready.ready, "the click covers this kind of action on this card, where it is named"
    assert ready.operation is not None
    rig.engine().do(ready.operation.handle)
    rig.engine().do(ready.operation.handle, "Sent. Reference BIN-7.")
    assert state_of_grant(rig, grant) is GrantState.ACTIVE, "a pattern is not used up"
    again = rig.engine().propose_intent(outside(what="Fill in a second form"), card=CARD)
    assert again.ready
    assert again.operation is not None
    rig.engine().do(again.operation.handle)
    third = outside(what="Fill in a third form")
    assert not rig.engine().propose_intent(third, card=CARD).ready, "two a day, as the click said"
    rig.clock.advance(hours=25)
    assert rig.engine().propose_intent(third, card=CARD).ready, "a new day"

    # Nothing else is covered: another card, another place, another kind, no card at all.
    for intent, card in (
        (outside(), other),
        (outside(where="Bank"), CARD),
        (outside(category="download"), CARD),
        (outside(), None),
    ):
        assert not rig.engine().propose_intent(intent, card=card).ready, (intent, card)

    # The owner sees the pattern in plain words, each use, and it all shows on the card's timeline.
    detail = grant_detail(rig.ctx, grant)
    assert "council portal" in detail["rendered"]
    assert "never covers entering a password" in (detail["rendered"])
    assert [use["state"] for use in detail["uses"]][-1].startswith("done, as reported")
    assert "BIN-7" in detail["uses"][-1]["reported"]
    items = [
        item
        for day in pages.timeline(rig.ctx, "UTC", None, 14)["days"]
        for item in day["items"]
        if item["kind"] == "outside"
    ]
    assert items
    assert all(item["card"] == "Bins" for item in items)
    assert "BIN-7" in text_of(items)
    assert "Council portal" in sections.verification(facts_of_pass(rig))

    rig.db.execute("UPDATE cards SET state = 'closed' WHERE handle = ?", (CARD,))
    with pytest.raises(ChamberlainError):  # a closed card cannot start anything new
        rig.engine().propose_intent(outside(), card=CARD)
    maintain(rig.ctx, rig.policy)
    assert state_of_grant(rig, grant) is GrantState.REVOKED


def state_of_grant(rig: WriteRig, handle: str) -> GrantState:
    found = repo.get_grant(rig.db, handle)
    assert found is not None
    return found.state


def text_of(items: list[dict[str, str]]) -> str:
    return " ".join(f"{i['title']} {i['detail']}" for i in items)


def test_one_standing_permission_covers_each_of_several_calendars_and_nothing_else(
    rig: WriteRig,
) -> None:
    scope = StandingScope(
        source=CALENDAR_B,
        sources=frozenset({CALENDAR, CALENDAR_B}),
        intent_kinds=frozenset({IntentKind.CALENDAR_CREATE}),
        title="Add appointments I committed to",
    )
    assert scope.source == CALENDAR, "the same set always gives the same scope"
    assert content_hash(scope) == content_hash(
        StandingScope(
            source=CALENDAR,
            sources=frozenset({CALENDAR_B, CALENDAR}),
            intent_kinds=frozenset({IntentKind.CALENDAR_CREATE}),
            title="Add appointments I committed to",
        )
    )
    with pytest.raises(ValidationError):  # one role only
        StandingScope(
            source=CALENDAR,
            sources=frozenset({CALENDAR, MAIL}),
            intent_kinds=frozenset({IntentKind.CALENDAR_CREATE}),
            title="x",
        )

    proposal = rig.engine().propose_standing(scope)
    assert proposal.grant is not None
    rig.human_approves(proposal)  # one click for both
    live = list_grants(rig.ctx, ["active"])
    assert len(live) == 1
    assert live[0]["sources"] == [CALENDAR, CALENDAR_B]
    assert "calendar:two" in grant_detail(rig.ctx, proposal.grant.handle)["rendered"]

    base: CalendarCreate = rig.event()
    for source in (CALENDAR, CALENDAR_B):
        ready = rig.engine().propose_intent(base.model_copy(update={"source": source}))
        assert ready.ready
        assert ready.grant is not None
        assert ready.grant.handle == proposal.grant.handle, source
    other = rig.engine().propose_intent(base.model_copy(update={"source": CALENDAR_PLAIN}))
    assert not other.ready, "a calendar the click did not name is not covered"
    withdrawn = rig.engine().grants.revoke(proposal.grant.handle, actor="human:viewer")
    assert withdrawn.state is GrantState.REVOKED
    for source in (CALENDAR, CALENDAR_B):
        gone = rig.engine().propose_intent(
            base.model_copy(
                update={
                    "source": source,
                    "event": base.event.model_copy(update={"title": "Another"}),
                }
            )
        )
        assert not gone.ready, "revoking ends it for every calendar it named"


def test_a_store_with_old_grants_migrates_without_changing_what_they_mean() -> None:
    """Migration 0230 rebuilds `operations`; grants keep their scope, hash and matches."""
    from chamberlain.ports.clock import ManualClock

    clock = ManualClock()
    db = connect(MEMORY)
    migrate(db, clock, backup_dir=None, migrations=[m for m in discover() if m.version < 230])
    scope = scope_for(MAIL)
    hash_before = content_hash(scope)
    stored = stamp(clock)
    db.execute(
        "INSERT INTO grants (handle, kind, state, source_id, title, requirement, content_sha256, "
        "scope_json, proposed_at, granted_at, expires_at, confirmed_via) "
        "VALUES ('grant_old000000001', 'standing', 'active', ?, 'Mark read', "
        "'human_confirmation', ?, ?, ?, ?, '2027-01-01T00:00:00Z', 'human_viewer')",
        (MAIL, hash_before, canonical_json(scope), stored, stored),
    )
    repo.insert_operation(
        db,
        handle="op_old0000000001",
        idempotency_key="k1",
        kind=IntentKind.MAIL_MARK_READ,
        source_id=MAIL,
        card=None,
        session=None,
        intent_sha256="a" * 64,
        intent_json="{}",
        refs_json="{}",
        at=stored,
    )
    db.execute("UPDATE operations SET state = 'confirmed' WHERE handle = 'op_old0000000001'")

    migrate(db, clock, backup_dir=None)

    grant = repo.get_grant(db, "grant_old000000001")
    assert grant is not None
    assert grant.state is GrantState.ACTIVE
    assert grant.scope is not None
    assert content_hash(grant.scope) == hash_before
    assert grant.scope.all_sources() == {MAIL}
    assert grant.scope.sources is None
    assert grant.content_sha256 == hash_before
    assert [g.handle for g in repo.grants_in_states(db, [GrantState.ACTIVE], MAIL)] == [
        grant.handle
    ]
    assert repo.get_operation(db, "op_old0000000001") is not None
    assert propose_missing(db, clock, _Audit(), [MAIL]) == [], "the owner is not asked again"
    db.close()


class _Audit:
    def append(self, **_: object) -> int:
        raise AssertionError("nothing should be proposed or written")
