"""Larger card limits the owner approves: docs/contracts/06-memory.md (M-36 to M-38).

The agent asks through the `card` tool; only the viewer's routes answer. Fake world only.
"""

from pathlib import Path
from typing import Any

import pytest
from viewer_rig import Rig

from chamberlain.attention.collect import collect
from chamberlain.attention.detectors.due import card_review_due
from chamberlain.limits import (
    CARD_MAX_CHARS,
    CARD_POSITION_MAX,
    COMPLEX_CARD_MAX_CHARS,
    COMPLEX_DECLINE_DAYS,
    COMPLEX_POSITION_MAX,
)
from chamberlain.writes import repo as writes_repo
from chamberlain.writes.audit import verify as audit_verify


class Agent:
    """The agent's side: tool calls through the wired app."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        started = rig.app.call("start", {"agent": "test"})
        assert started.ok
        self.session = str(started.data.session)  # type: ignore[union-attr]

    def call(self, tool: str, request: dict[str, Any]) -> Any:
        return self.rig.app.call(tool, {**request, "session": self.session})

    def card(self, title: str = "A demanding matter", position: str = "") -> str:
        fields: dict[str, str] = {"title": title}
        if position:
            fields["position"] = position
        made = self.call("card", {"op": "create", "fields": fields})
        assert made.ok
        return str(made.data.card.handle)

    def ask(self, card: str, reason: str = "Several parties and a long history.") -> Any:
        return self.call("card", {"op": "update", "card": card, "complex_reason": reason})

    def set_position(self, card: str, text: str) -> Any:
        return self.call("card", {"op": "update", "card": card, "fields": {"position": text}})


@pytest.fixture
def agent(rig: Rig) -> Agent:
    return Agent(rig)


def audit_kinds(rig: Rig) -> list[str]:
    return [r.kind for r in writes_repo.audit_rows(rig.ctx.db)]


def test_m36_a_request_is_pending_and_changes_nothing_until_the_owner_answers(
    rig: Rig, agent: Agent
) -> None:
    card = agent.card()
    reply = agent.ask(card)
    assert reply.ok
    assert reply.data.card.complex == "pending"
    too_long = "p" * (CARD_POSITION_MAX + 1)
    refused = agent.set_position(card, too_long)
    assert not refused.ok
    assert refused.error.code.value == "card.over_cap"
    assert any(g.id == "card.complex" for g in refused.guidance)  # the one tiny rule
    waiting = rig.call("GET", "/api/complex").body
    assert [p["card"] for p in waiting["pending"]] == [card]
    assert waiting["pending"][0]["reason"] == "Several parties and a long history."
    assert "card.complex_requested" in audit_kinds(rig)


def test_m36_approval_raises_the_caps_to_4000_and_6000(rig: Rig, agent: Agent) -> None:
    card = agent.card()
    agent.ask(card)
    done = rig.call("POST", f"/api/complex/{card}/approve", {})
    assert done.status == 200
    assert agent.set_position(card, "p" * COMPLEX_POSITION_MAX).ok
    over = agent.set_position(card, "p" * (COMPLEX_POSITION_MAX + 1))
    assert not over.ok
    shown = agent.call("open", {"handle": card}).data.view
    assert shown.complex == "approved"
    # the card total may reach 6,000 but not pass it
    assert agent.call("card", {"op": "update", "card": card, "fields": {"goal": "g" * 400}}).ok
    assert agent.call("card", {"op": "update", "card": card, "fields": {"rules": "r" * 600}}).ok
    refusal = None
    for number in range(7):
        added = agent.call(
            "card",
            {"op": "add_action", "card": card, "action_fields": {"title": f"{number}" + "t" * 159}},
        )
        if not added.ok:
            refusal = added
            break
    assert refusal is not None
    assert refusal.error.code.value == "card.over_cap"
    assert f"{COMPLEX_CARD_MAX_CHARS}" in refusal.error.message
    final = agent.call("open", {"handle": card}).data.view
    total = len(final.model_dump_json(exclude_none=True, exclude={"context", "complex"}))
    assert CARD_MAX_CHARS < total <= COMPLEX_CARD_MAX_CHARS


def test_m36_the_caps_of_a_normal_card_do_not_change_for_other_cards(
    rig: Rig, agent: Agent
) -> None:
    first, second = agent.card("First"), agent.card("Second")
    agent.ask(first)
    rig.call("POST", f"/api/complex/{first}/approve", {})
    assert not agent.set_position(second, "p" * (CARD_POSITION_MAX + 1)).ok


def test_m37_there_is_no_quota_the_owner_decides_each_card(rig: Rig, agent: Agent) -> None:
    """Owner ruling 2026-11-24: no cap on how many cards may hold a larger limit."""
    cards = [agent.card(f"Matter {n}") for n in range(5)]
    for card in cards:
        assert agent.ask(card).ok
    for card in cards:
        assert rig.call("POST", f"/api/complex/{card}/approve", {}).status == 200
    overview = rig.call("GET", "/api/complex").body
    assert sorted(a["card"] for a in overview["approved"]) == sorted(cards)
    assert "limit" not in overview


def test_m37_a_decline_tells_the_agent_and_blocks_asking_again_for_30_days(
    rig: Rig, agent: Agent
) -> None:
    card = agent.card()
    agent.ask(card)
    assert rig.call("POST", f"/api/complex/{card}/decline", {}).status == 200
    shown = agent.call("open", {"handle": card}).data.view
    assert shown.complex == "declined"
    again = agent.ask(card)
    assert not again.ok
    assert "declined" in again.error.message
    events = [e for e in card_review_due(collect(rig.ctx).data) if "declined" in e.summary]
    assert len(events) == 1
    assert card in events[0].summary
    assert events[0].attrs.reason == "complex_declined"
    rig.clock.advance(hours=24 * (COMPLEX_DECLINE_DAYS - 1))
    assert not agent.ask(card).ok
    rig.clock.advance(hours=24 * 2)
    assert agent.ask(card).ok  # a new request after the 30 days
    assert "card.complex_declined" in audit_kinds(rig)


def test_m37_an_approval_is_told_to_the_agent_too(rig: Rig, agent: Agent) -> None:
    card = agent.card()
    agent.ask(card)
    rig.call("POST", f"/api/complex/{card}/approve", {})
    events = [
        e for e in card_review_due(collect(rig.ctx).data) if e.attrs.reason == "complex_approved"
    ]
    assert [e.subject for e in events] == [card]


def test_m38_revoking_returns_the_card_to_normal_caps_and_the_over_cap_flow_applies(
    rig: Rig, agent: Agent
) -> None:
    card = agent.card()
    agent.ask(card)
    rig.call("POST", f"/api/complex/{card}/approve", {})
    assert agent.set_position(card, "p" * 3000).ok
    revoked = rig.call("POST", f"/api/complex/{card}/revoke", {})
    assert revoked.status == 200
    assert revoked.body["over_normal_limit"] is True
    row = rig.rows("SELECT over_cap, review_at FROM cards WHERE handle = ?", card)[0]
    assert row["over_cap"] == 1
    assert row["review_at"] == "2026-01-05T09:00:00Z"
    reasons = [e.attrs.reason for e in card_review_due(collect(rig.ctx).data)]
    assert "over_cap" in reasons
    still = agent.set_position(card, "p" * 3000)
    assert not still.ok  # must come under the normal cap
    assert agent.set_position(card, "p" * 1000).ok
    assert rig.rows("SELECT over_cap FROM cards WHERE handle = ?", card)[0]["over_cap"] == 0
    assert "card.complex_revoked" in audit_kinds(rig)


def test_m38_revoking_a_card_within_the_normal_caps_flags_nothing(rig: Rig, agent: Agent) -> None:
    card = agent.card()
    agent.ask(card)
    rig.call("POST", f"/api/complex/{card}/approve", {})
    revoked = rig.call("POST", f"/api/complex/{card}/revoke", {})
    assert revoked.body["over_normal_limit"] is False
    assert rig.rows("SELECT over_cap FROM cards WHERE handle = ?", card)[0]["over_cap"] == 0


def test_m36_every_step_is_in_the_audit_chain_with_the_right_actor(rig: Rig, agent: Agent) -> None:
    card = agent.card()
    agent.ask(card)
    rig.call("POST", f"/api/complex/{card}/approve", {})
    rig.call("POST", f"/api/complex/{card}/revoke", {})
    other = agent.card("Other")
    agent.ask(other)
    rig.call("POST", f"/api/complex/{other}/decline", {})
    rows = [r for r in writes_repo.audit_rows(rig.ctx.db) if r.kind.startswith("card.complex")]
    assert [r.kind for r in rows] == [
        "card.complex_requested",
        "card.complex_approved",
        "card.complex_revoked",
        "card.complex_requested",
        "card.complex_declined",
    ]
    assert {r.actor for r in rows if r.kind != "card.complex_requested"} == {"human:viewer"}
    assert all(r.actor.startswith("agent:") for r in rows if r.kind == "card.complex_requested")
    assert audit_verify(rig.ctx.db).ok


def test_m36_the_agent_cannot_approve_its_own_request(rig: Rig, agent: Agent) -> None:
    card = agent.card()
    agent.ask(card)
    # no tool offers an answer, and asking again never changes a pending request
    assert agent.ask(card, "Please approve this at once.").data.card.complex == "pending"
    rejected = agent.call(
        "card", {"op": "update", "card": card, "complex_reason": "x", "fields": {"title": "y"}}
    )
    assert not rejected.ok
    status = rig.rows("SELECT state FROM complex_cards WHERE card = ?", card)[0]["state"]
    assert status == "pending"
    for tool_request in (
        {"op": "update", "card": card, "fields": {"position": "p" * (CARD_POSITION_MAX + 1)}},
    ):
        assert not agent.call("card", tool_request).ok


def test_m36_only_the_viewer_module_can_decide(tmp_path: Path) -> None:
    """The deciding methods are reached only from the viewer, never from a tool handler."""
    import chamberlain

    root = Path(chamberlain.__file__).parent
    callers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if any(
            name in path.read_text(encoding="utf-8")
            for name in (".approve(", ".decline(", ".revoke(")
        )
        and "ComplexCards" in path.read_text(encoding="utf-8")
    }
    assert callers <= {"surface/viewer/complex_api.py", "memory/complex_cards.py"}


def test_m36_the_routes_need_the_token_and_the_viewers_origin(rig: Rig, agent: Agent) -> None:
    card = agent.card()
    agent.ask(card)
    path = f"/api/complex/{card}/approve"
    assert rig.call("POST", path, {}, token=None).status == 403
    assert rig.call("POST", path, {}, token="wrong").status == 403
    assert rig.call("POST", path, {}, origin="http://evil.example").status == 403
    assert rig.call("GET", "/api/complex", token=None).status == 403
    assert rig.rows("SELECT state FROM complex_cards WHERE card = ?", card)[0]["state"] == "pending"


def test_m36_an_answer_to_nothing_is_refused(rig: Rig, agent: Agent) -> None:
    card = agent.card()
    assert rig.call("POST", f"/api/complex/{card}/approve", {}).status == 400
    assert rig.call("POST", f"/api/complex/{card}/revoke", {}).status == 400
    assert rig.call("POST", "/api/complex/card_0123456789abcdef/approve", {}).status == 400


def test_m36_the_cards_page_shows_which_cards_are_complex_and_why(rig: Rig, agent: Agent) -> None:
    waiting, held, plain = agent.card("Waiting"), agent.card("Held"), agent.card("Plain")
    agent.ask(waiting, "Still waiting.")
    agent.ask(held, "Many parties.")
    rig.call("POST", f"/api/complex/{held}/approve", {})
    items = {c["title"]: c for c in rig.call("GET", "/api/cards").body["cards"]}
    assert items["Plain"]["complex"] is None
    assert items["Held"]["complex"] == {"state": "approved", "reason": "Many parties."}
    assert items["Waiting"]["complex"]["state"] == "pending"
    assert plain in [c["handle"] for c in items.values()]
    overview = rig.call("GET", "/api/complex").body
    assert [a["card"] for a in overview["approved"]] == [held]


def test_m36_closing_a_card_ends_its_larger_limit(rig: Rig, agent: Agent) -> None:
    card = agent.card("Matter")
    agent.ask(card)
    rig.call("POST", f"/api/complex/{card}/approve", {})
    closed = agent.call("card", {"op": "close", "card": card, "outcome": "Settled."})
    assert closed.ok
    assert rig.call("GET", "/api/complex").body["approved"] == []


def test_m36_the_viewer_scripts_are_served_and_hold_no_markup_parsing(rig: Rig) -> None:
    assert rig.call("GET", "/static/complex.js", token=None).status == 200
    script = rig.last_bytes.decode()
    assert "innerHTML" not in script
    assert "Approve" in script
    rig.call("GET", "/", token=None)
    assert "/static/complex.js" in rig.last_bytes.decode()
