"""Handles, canonical form, intents and state tables: 01-domain.md, 04-writes.md, 05-grants.md."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from chamberlain.domain.canonical import canonical_json, content_hash
from chamberlain.domain.grants import (
    GRANT_TRANSITIONS,
    LIVE_GRANT_STATES,
    TERMINAL_GRANT_STATES,
    GrantState,
    StandingScope,
)
from chamberlain.domain.handles import HandleKind, handle_kind, new_handle, ref_handle
from chamberlain.domain.intents import IntentKind, MailMove, MailSend, WriteIntent
from chamberlain.domain.operations import (
    MAY_HAVE_HAPPENED_STATES,
    RELEASING_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    DispatchOutcome,
    DispatchResult,
    OperationState,
    ReconcileResult,
    ReconcileStatus,
    check_transition,
)
from chamberlain.schema.base import HANDLE_PREFIXES
from chamberlain.schema.errors import ChamberlainError, ErrorCode

INTENT = TypeAdapter[WriteIntent](WriteIntent)
SEND = {
    "kind": "mail.send",
    "source": "mail:work",
    "to": ["Someone@Example.org"],
    "subject": "Friday",
    "body": "Friday works.",
}


def test_d3_handle_prefixes_match_the_schema() -> None:
    assert {kind.value for kind in HandleKind} == set(HANDLE_PREFIXES)


@given(st.sampled_from([k for k in HandleKind if k.value not in {"msg", "cev", "con"}]))
def test_d3_new_handles_are_opaque_and_recognised(kind: HandleKind) -> None:
    handle = new_handle(kind)
    assert handle_kind(handle) is kind
    assert handle != new_handle(kind)


def test_d6_ref_handles_are_deterministic_and_hide_the_locator() -> None:
    first = ref_handle(HandleKind.MESSAGE, "mail:work", "f-inbox/<secret-id@example.org>")
    again = ref_handle(HandleKind.MESSAGE, "mail:work", "f-inbox/<secret-id@example.org>")
    other = ref_handle(HandleKind.MESSAGE, "mail:home", "f-inbox/<secret-id@example.org>")
    assert first == again != other
    assert "secret" not in first
    assert handle_kind(first) is HandleKind.MESSAGE


@pytest.mark.parametrize(
    "text", ["", "card", "card_", "card_SHOUTING1", "../etc/passwd", "x_12345678"]
)
def test_d3_things_that_are_not_handles_are_refused(text: str) -> None:
    with pytest.raises(ChamberlainError) as raised:
        handle_kind(text)
    assert raised.value.code is ErrorCode.REQUEST_UNKNOWN_HANDLE


def test_d14_canonical_form_ignores_key_order_and_omits_none() -> None:
    forward = INTENT.validate_python(SEND)
    backward = INTENT.validate_python(dict(reversed(list(SEND.items()))))
    assert canonical_json(forward) == canonical_json(backward)
    assert content_hash(forward) == content_hash(backward)
    assert "message_id" not in canonical_json(forward)
    assert " " not in canonical_json(forward).replace("Friday works.", "")


def test_w18_any_change_to_an_intent_changes_its_hash() -> None:
    base = INTENT.validate_python(SEND)
    for change in ({"body": "Friday works!"}, {"cc": ["x@example.org"]}, {"subject": "Monday"}):
        assert content_hash(INTENT.validate_python({**SEND, **change})) != content_hash(base)


def test_d15_recipients_are_bare_lowercased_addresses() -> None:
    intent = INTENT.validate_python(SEND)
    assert isinstance(intent, MailSend)
    assert intent.to == ("someone@example.org",)
    with pytest.raises(ValidationError):
        INTENT.validate_python({**SEND, "to": ["Someone <someone@example.org>"]})


def test_w5_a_send_needs_a_recipient_a_subject_and_no_line_breaks() -> None:
    for bad in ({"to": []}, {"subject": ""}, {"subject": "a\r\nBcc: x@example.org"}):
        with pytest.raises(ValidationError):
            INTENT.validate_python({**SEND, **bad})


def test_w1_intents_are_discriminated_by_kind_and_closed() -> None:
    move = INTENT.validate_python(
        {
            "kind": "mail.move",
            "source": "mail:work",
            "message": "msg_0123456789abcdef",
            "to_folder": "f-filed",
        }
    )
    assert isinstance(move, MailMove)
    assert not move.mark_read
    with pytest.raises(ValidationError):
        INTENT.validate_python({"kind": "mail.delete_forever", "source": "mail:work"})
    with pytest.raises(ValidationError):
        INTENT.validate_python({**SEND, "delay_minutes": 5})


def test_w6_the_transition_table_is_total_and_terminal_states_are_final() -> None:
    assert set(TRANSITIONS) == set(OperationState)
    assert {
        OperationState.CONFIRMED,
        OperationState.FAILED_CLEAN,
        OperationState.CONFIRMED_ABSENT,
        OperationState.CANCELLED,
        OperationState.ABANDONED,
    } == TERMINAL_STATES
    assert RELEASING_STATES < TERMINAL_STATES
    assert not MAY_HAVE_HAPPENED_STATES & TERMINAL_STATES


def test_w8_nothing_leads_back_to_dispatched() -> None:
    """No path re-dispatches: only `prepared` reaches `dispatched`."""
    sources = {s for s, targets in TRANSITIONS.items() if OperationState.DISPATCHED in targets}
    assert sources == {OperationState.PREPARED}
    reachable = _reachable(OperationState.DISPATCHED)
    assert OperationState.PREPARED not in reachable
    assert OperationState.DISPATCHED not in reachable


def test_w8_uncertain_cannot_become_failed_clean_without_proof_of_absence() -> None:
    assert OperationState.FAILED_CLEAN not in _reachable(OperationState.UNCERTAIN)


def test_w6_illegal_transitions_raise() -> None:
    check_transition(OperationState.PREPARED, OperationState.DISPATCHED)
    with pytest.raises(ChamberlainError):
        check_transition(OperationState.UNCERTAIN, OperationState.DISPATCHED)
    with pytest.raises(ChamberlainError):
        check_transition(OperationState.CONFIRMED, OperationState.UNCERTAIN)


def test_p10_p11_results_carry_a_receipt_exactly_when_something_is_there() -> None:
    with pytest.raises(ValidationError):
        DispatchResult(outcome=DispatchOutcome.APPLIED)
    with pytest.raises(ValidationError):
        ReconcileResult(status=ReconcileStatus.PRESENT)
    with pytest.raises(ValidationError):
        ReconcileResult(status=ReconcileStatus.UNKNOWN, proof=True)
    assert ReconcileResult(status=ReconcileStatus.ABSENT, proof=True).proof


def test_g2_g4_grant_states_never_return_from_terminal() -> None:
    assert set(GRANT_TRANSITIONS) == set(GrantState)
    assert GrantState.SPENT in TERMINAL_GRANT_STATES
    assert not LIVE_GRANT_STATES & TERMINAL_GRANT_STATES
    for state in TERMINAL_GRANT_STATES:
        assert not GRANT_TRANSITIONS[state]


def test_g1_a_standing_send_scope_has_no_wildcards() -> None:
    scope = {"source": "mail:work", "intent_kinds": ["mail.send"], "title": "Weekly report"}
    with pytest.raises(ValidationError):
        StandingScope.model_validate(scope)
    with pytest.raises(ValidationError):
        StandingScope.model_validate({**scope, "recipients": ["boss@example.org"]})
    ok = StandingScope.model_validate(
        {**scope, "recipients": ["boss@example.org"], "max_per_day": 1}
    )
    assert IntentKind.MAIL_SEND in ok.intent_kinds
    with pytest.raises(ValidationError):
        StandingScope.model_validate({**scope, "intent_kinds": []})


def _reachable(start: OperationState) -> set[OperationState]:
    seen: set[OperationState] = set()
    frontier = list(TRANSITIONS[start])
    while frontier:
        state = frontier.pop()
        if state not in seen:
            seen.add(state)
            frontier.extend(TRANSITIONS[state])
    return seen
