"""`authorise` is pure and never allows what it should not (04-writes.md W-20 to W-22, G-16)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from chamberlain.domain.grants import GrantKind, GrantState, Requirement, StandingScope
from chamberlain.domain.intents import IntentKind, MailSend
from chamberlain.schema.errors import ErrorCode
from chamberlain.writes.authorise import (
    Allow,
    Decision,
    Deny,
    OperationFacts,
    Usage,
    authorise,
)
from chamberlain.writes.models import GrantRow, WritePolicy
from chamberlain.writes.rig import MAIL

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
SEND = MailSend(source=MAIL, to=("friend@example.org",), subject="s", body="b")
HASH = "a" * 64
OTHER_HASH = "b" * 64
POLICY = WritePolicy()
FACTS = OperationFacts(handle="op_0123456789abcdef", intent_sha256=HASH)


def one_off(**kwargs: object) -> GrantRow:
    return grant(kind=GrantKind.ONE_OFF, content_sha256=HASH, scope=None, **kwargs)


def standing(max_per_day: int | None = 2, **kwargs: object) -> GrantRow:
    scope = StandingScope(
        source=MAIL,
        intent_kinds=frozenset({IntentKind.MAIL_SEND}),
        title="t",
        recipients=frozenset({"friend@example.org"}),
        max_per_day=max_per_day,
    )
    return grant(kind=GrantKind.STANDING, content_sha256=OTHER_HASH, scope=scope, **kwargs)


def grant(**kwargs: object) -> GrantRow:
    base: dict[str, object] = {
        "handle": "grant_0123456789abcdef",
        "kind": GrantKind.ONE_OFF,
        "state": GrantState.ACTIVE,
        "source_id": MAIL,
        "title": "t",
        "summary": "",
        "requirement": Requirement.AGENT_EVIDENCE,
        "content_sha256": HASH,
        "scope": None,
        "card": None,
        "supersedes": None,
        "proposed_by": None,
        "proposed_at": NOW - timedelta(days=1),
        "confirmed_via": "human_viewer",
        "granted_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=5),
        "review_at": None,
        "last_reviewed_at": None,
        "ended_at": None,
        "compacted_at": None,
    }
    return GrantRow(**{**base, **kwargs})  # type: ignore[arg-type]


def decide(
    grants: list[GrantRow], usage: Usage | None = None, facts: OperationFacts = FACTS
) -> Decision:
    return authorise(SEND, facts, grants, usage or Usage(), POLICY, NOW)


def test_w20_the_same_inputs_give_the_same_decision() -> None:
    grants = [one_off(), standing()]
    assert decide(grants) == decide(grants)
    assert decide(grants) == Allow("grant_0123456789abcdef")


def test_w22_a_one_off_for_exactly_this_content_beats_a_standing_grant() -> None:
    chosen = one_off(handle="grant_oneoff0000000a")
    broad = standing(handle="grant_standing000000a", review_at=NOW + timedelta(days=9))
    decision = decide([broad, chosen])
    assert isinstance(decision, Allow)
    assert decision.grant == "grant_oneoff0000000a"


def test_w22_the_narrowest_covering_standing_grant_is_used() -> None:
    wide = standing(handle="grant_wide000000000a")
    scope = StandingScope(
        source=MAIL,
        intent_kinds=frozenset({IntentKind.MAIL_SEND}),
        title="n",
        recipients=frozenset({"friend@example.org"}),
        max_per_day=2,
    )
    narrow_row = grant(
        handle="grant_narrow00000000a",
        kind=GrantKind.STANDING,
        content_sha256=OTHER_HASH,
        scope=scope,
    )
    wide_scope = scope.model_copy(
        update={"recipients": frozenset({"friend@example.org", "x@y.org"})}
    )
    wider = replace(wide, scope=wide_scope)
    decision = decide([wider, narrow_row])
    assert isinstance(decision, Allow)
    assert decision.grant == "grant_narrow00000000a"


def test_g2_g8_only_an_active_unexpired_grant_authorises() -> None:
    for state in GrantState:
        if state is GrantState.ACTIVE:
            continue
        decision = decide([one_off(state=state)])
        assert isinstance(decision, Deny)
    expired = decide([one_off(expires_at=NOW)])
    assert isinstance(expired, Deny)
    assert expired.code is ErrorCode.GRANT_NOT_ACTIVE


def test_g8_an_overdue_standing_grant_authorises_nothing_after_the_grace_period() -> None:
    fresh = standing(review_at=NOW - timedelta(days=13))
    stale = standing(review_at=NOW - timedelta(days=14))
    assert isinstance(decide([fresh]), Allow)
    assert isinstance(decide([stale]), Deny)


def test_w21_a_reserved_grant_that_lapsed_since_denies() -> None:
    reserved = replace(FACTS, reserved_grant="grant_0123456789abcdef")
    assert isinstance(decide([one_off()], facts=reserved), Allow)
    denied = decide([one_off(state=GrantState.REVOKED)], facts=reserved)
    assert isinstance(denied, Deny)
    assert denied.code is ErrorCode.GRANT_NOT_ACTIVE
    mismatch = replace(reserved, intent_sha256=OTHER_HASH)
    changed = decide([one_off()], facts=mismatch)
    assert isinstance(changed, Deny)
    assert changed.code is ErrorCode.GRANT_CONTENT_MISMATCH


def test_g11_a_one_off_with_a_live_use_is_unavailable() -> None:
    used = Usage(live_uses={"grant_0123456789abcdef": 1})
    denied = decide([one_off()], used)
    assert isinstance(denied, Deny)
    assert denied.code is ErrorCode.GRANT_LIMIT_REACHED


def test_g16_standing_daily_and_hourly_limits() -> None:
    handle = "grant_0123456789abcdef"
    assert isinstance(decide([standing(2)], Usage(recent_uses={handle: 1})), Allow)
    full = decide([standing(2)], Usage(recent_uses={handle: 2}))
    assert isinstance(full, Deny)
    assert full.code is ErrorCode.GRANT_LIMIT_REACHED
    busy = decide([one_off()], Usage(source_sends_last_hour=20))
    assert isinstance(busy, Deny)
    assert busy.code is ErrorCode.GRANT_LIMIT_REACHED
    assert isinstance(decide([one_off()], Usage(source_sends_last_hour=19)), Allow)


def test_g15_an_unknown_recipient_is_reported_and_needs_the_human() -> None:
    denied = decide([])
    assert isinstance(denied, Deny)
    assert denied.code is ErrorCode.RECIPIENT_UNKNOWN
    assert denied.requirement is Requirement.HUMAN_CONFIRMATION
    known = decide([], Usage(known_recipients=frozenset({"friend@example.org"})))
    assert isinstance(known, Deny)
    assert known.code is ErrorCode.GRANT_REQUIRED
    assert known.requirement is Requirement.AGENT_EVIDENCE


grant_strategy = st.builds(
    lambda kind, state, content_same, expires, review, max_per_day, other_source: grant(
        handle=f"grant_{kind.value}{state.value}"[:30].ljust(14, "0"),
        kind=kind,
        state=state,
        content_sha256=HASH if content_same else OTHER_HASH,
        scope=None
        if kind is GrantKind.ONE_OFF
        else StandingScope(
            source=MAIL,
            intent_kinds=frozenset({IntentKind.MAIL_SEND}),
            title="t",
            recipients=frozenset({"friend@example.org"}),
            max_per_day=max_per_day,
        ),
        expires_at=NOW + timedelta(hours=expires),
        review_at=None if review is None else NOW + timedelta(days=review),
        source_id="mail:other" if other_source else MAIL,
    ),
    st.sampled_from(GrantKind),
    st.sampled_from(GrantState),
    st.booleans(),
    st.integers(min_value=-48, max_value=48),
    st.one_of(st.none(), st.integers(min_value=-40, max_value=40)),
    st.integers(min_value=1, max_value=4),
    st.booleans(),
)


@given(
    grants=st.lists(grant_strategy, max_size=4, unique_by=lambda g: g.handle),
    recent=st.integers(min_value=0, max_value=6),
    live=st.integers(min_value=0, max_value=2),
    hour=st.integers(min_value=0, max_value=25),
)
def test_w20_property_never_allows_what_the_grant_does_not_cover(
    grants: list[GrantRow], recent: int, live: int, hour: int
) -> None:
    usage = Usage(
        live_uses={g.handle: live for g in grants},
        recent_uses={g.handle: recent for g in grants},
        source_sends_last_hour=hour,
    )
    first = decide(grants, usage)
    assert first == decide(grants, usage)
    if isinstance(first, Deny):
        return
    chosen = next(g for g in grants if g.handle == first.grant)
    assert chosen.state is GrantState.ACTIVE
    assert chosen.source_id == SEND.source
    assert chosen.expires_at is not None
    assert chosen.expires_at > NOW
    assert hour < POLICY.sends_per_hour
    if chosen.kind is GrantKind.ONE_OFF:
        assert chosen.content_sha256 == HASH
        assert live == 0
    else:
        assert chosen.scope is not None
        assert chosen.scope.max_per_day is not None
        assert recent < chosen.scope.max_per_day
        assert chosen.review_at is None or NOW < chosen.review_at + timedelta(days=14)
