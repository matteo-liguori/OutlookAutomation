"""The prompt and one full mail pass stay inside the budgets of docs/contracts/10-targets.md.

The pass is replayed over the fake provider through the real wiring: four unread
messages are read, carded, two answered, two marked read, and the cards closed. The
numbers it measures are printed (`pytest -s`) and recorded on the board as the baseline
for wave 2; the assertions are the targets, not the baseline.
"""

import json
from typing import Any

from tests.integration.conftest import MAIL, Stack

from chamberlain.limits import (
    GUIDANCE_REPLY_CHARS,
    GUIDANCE_TOTAL_CHARS,
    MAX_REPLY_CHARS,
    PAGE_CHARS,
    STANDING_PROMPT_MAX,
    TOOL_COUNT,
)
from chamberlain.schema.registry import STANDING_PROMPT, TOOLS

GUIDANCE_PER_PASS_CHARS = 1_500
"""10-targets.md: guidance per typical mail pass."""
FRIENDS = ("ann@example.org", "bob@example.org", "cat@example.org", "dan@example.org")


class Measured:
    """Every reply of a pass, measured the way an agent would pay for it."""

    def __init__(self, stack: Stack) -> None:
        self._stack = stack
        self.replies: list[int] = []
        self.guidance_text: list[int] = []
        self.tools: list[str] = []

    def ok(self, tool: str, **request: Any) -> dict[str, Any]:
        reply = self._stack.call(tool, **request)
        assert reply["ok"], (tool, reply.get("error"))
        self.replies.append(len(json.dumps(reply, ensure_ascii=False)))
        self.guidance_text.append(sum(len(g.get("text", "")) for g in reply.get("guidance", [])))
        self.tools.append(tool)
        data: dict[str, Any] = reply["data"]
        return data

    @property
    def total_chars(self) -> int:
        return sum(self.replies)

    @property
    def guidance_chars(self) -> int:
        return sum(self.guidance_text)


def run_pass(stack: Stack) -> Measured:
    for index, friend in enumerate(FRIENDS):
        stack.fake.mail.add_message(
            MAIL,
            internet_message_id=f"<pass{index}@example.org>",
            subject=f"Question {index} about the plan",
            sender=friend,
            body="Could you tell me whether the plan still holds? " * 8,
        )
        stack.fake.mail.add_message(
            MAIL,
            internet_message_id=f"<seen{index}@example.org>",
            subject=f"Old thread {index}",
            sender=friend,
            body="Already dealt with.",
            unread=False,
        )
    stack.know(*FRIENDS)
    seen = Measured(stack)
    started = seen.ok("start", agent="test")
    stack.session = started["session"]
    handled = [item["event"] for item in started["work"]]
    hits = seen.ok("find", scope="mail", unread=True)["hits"]
    assert len(hits) == len(FRIENDS)
    answered = 0
    for index, hit in enumerate(hits):
        message = hit["handle"]
        seen.ok("open", handle=message, part="body")
        card = seen.ok("card", op="create", fields={"title": f"Plan question {index}"})["card"]
        if index % 2 == 0:
            answered += 1
            intent: dict[str, Any] = {
                "kind": "mail.send",
                "source": MAIL,
                "to": [FRIENDS[index]],
                "subject": f"Re: Question {index} about the plan",
                "body": "Yes, the plan still holds.",
                "reply_to": message,
            }
        else:
            intent = {"kind": "mail.mark_read", "source": MAIL, "messages": [message]}
        proposal = seen.ok("propose", intent=intent, card=card["handle"])
        words = seen.ok(
            "keep",
            kind="evidence",
            owner=proposal["operation"],
            text="Owner: yes, go ahead.",
            reason="Approval.",
        )
        if proposal["requirement"] == "agent_evidence":
            seen.ok("propose", operation=proposal["operation"], approval_evidence=words["handle"])
            seen.ok("do", operation=proposal["operation"])
        seen.ok("card", op="close", card=card["handle"], outcome="Dealt with.")
    seen.ok("next", handled=handled[:10])
    assert answered == 2
    return seen


def test_t1_the_standing_prompt_and_the_tool_list_are_inside_their_budgets() -> None:
    assert len(STANDING_PROMPT) <= STANDING_PROMPT_MAX
    assert len(TOOLS) == TOOL_COUNT


def test_a14_a_full_mail_pass_stays_inside_the_reply_and_guidance_budgets(stack: Stack) -> None:
    seen = run_pass(stack)
    biggest = max(seen.replies)
    per_reply_guidance = max(seen.guidance_text)
    tokens = (seen.total_chars + len(STANDING_PROMPT)) // 4
    print(
        f"BUDGET prompt={len(STANDING_PROMPT)} tools={len(TOOLS)} calls={len(seen.replies)} "
        f"reply_total={seen.total_chars} reply_max={biggest} "
        f"guidance_total={seen.guidance_chars} guidance_max_reply={per_reply_guidance} "
        f"tokens_est={tokens}"
    )
    assert biggest <= MAX_REPLY_CHARS
    assert per_reply_guidance <= GUIDANCE_REPLY_CHARS
    assert seen.guidance_chars < GUIDANCE_PER_PASS_CHARS
    metrics = stack.ok("doctor", metrics=True)["metrics"]
    assert metrics["largest_reply_chars_7d"] <= MAX_REPLY_CHARS
    assert metrics["guidance_total_chars"] < GUIDANCE_TOTAL_CHARS
    assert PAGE_CHARS < MAX_REPLY_CHARS
