"""Rule selection for one reply: the six steps of the contract.

Contract: docs/contracts/07-attention.md (Selection, A-15, A-16). Pure and
deterministic: the same situation and ledger give the same rules in the same order.
"""

from dataclasses import dataclass
from typing import Any

from chamberlain.attention.models import EventRow
from chamberlain.attention.rules import Rule, Trigger, result_matches
from chamberlain.limits import (
    GUIDANCE_POINTER_COST,
    GUIDANCE_POINTERS_MAX,
    GUIDANCE_REPLY_CHARS,
)

DISPLACE_PRIORITY = 90
"""A rule at or above this priority is placed before any lower one (A-16)."""


@dataclass(frozen=True)
class Situation:
    """What a reply is about: the call, the reply and the events it delivers or concerns."""

    tool: str
    request: dict[str, Any]
    reply: dict[str, Any]
    error_code: str | None
    error_category: str | None
    events: tuple[EventRow, ...]


@dataclass(frozen=True)
class Chosen:
    rule: Rule
    scopes: tuple[str, ...]
    """Ledger scope keys this delivery covers."""


@dataclass(frozen=True)
class Selected:
    full: tuple[Chosen, ...]
    pointers: tuple[Rule, ...]


def _filters_match(
    filters: tuple[tuple[str, tuple[str, ...]], ...], values: dict[str, Any]
) -> bool:
    for name, wanted in filters:
        actual = values.get(name)
        if actual is None or str(getattr(actual, "value", actual)) not in wanted:
            return False
    return True


def _scope(rule: Rule, situation: Situation, event: EventRow | None) -> str:
    once = rule.once_per
    if once == "event" and event is not None:
        return event.handle
    if once == "operation":
        if event is not None and (event.subject or "").startswith("op_"):
            return event.subject or "session"
        named = situation.request.get("operation")
        return named if isinstance(named, str) else "session"
    if once == "card":
        if event is not None:
            if event.attrs.card is not None:
                return event.attrs.card
            if (event.subject or "").startswith("card_"):
                return event.subject or "session"
        named = situation.request.get("card")
        return named if isinstance(named, str) else "session"
    return "session"


def _event_attr_values(event: EventRow) -> dict[str, Any]:
    return event.attrs.model_dump(exclude_none=True)


def matching_scopes(rule: Rule, situation: Situation, trigger: Trigger) -> list[str]:
    """Scope keys for which the rule's trigger matches this situation (step 1)."""
    if trigger.kind == "event":
        return sorted(
            {
                _scope(rule, situation, event)
                for event in situation.events
                if event.kind.value == trigger.value
                and _filters_match(trigger.filters, _event_attr_values(event))
            }
        )
    if trigger.kind == "tool":
        if situation.tool == trigger.value and _filters_match(trigger.filters, situation.request):
            return [_scope(rule, situation, None)]
        return []
    if trigger.kind == "result":
        return [_scope(rule, situation, None)] if result_matches(trigger, situation.reply) else []
    wanted = trigger.value
    if wanted in (situation.error_code, situation.error_category):
        return [_scope(rule, situation, None)]
    return []


def select(
    rules: tuple[Rule, ...],
    situation: Situation,
    delivered: dict[tuple[str, str], int],
    *,
    budget: int = GUIDANCE_REPLY_CHARS,
) -> Selected:
    """Steps 1 to 5. Step 6, recording deliveries, belongs to the caller."""
    eligible: list[Chosen] = []
    for rule in rules:
        trigger = rule.trigger
        if trigger is None:
            continue
        scopes = matching_scopes(rule, situation, trigger)
        if rule.once_per != "always":
            scopes = [s for s in scopes if delivered.get((rule.id, s)) != rule.version]
        if scopes:
            eligible.append(Chosen(rule, tuple(scopes)))
    eligible.sort(key=lambda c: (-c.rule.priority, c.rule.id))
    urgent = [c for c in eligible if c.rule.priority >= DISPLACE_PRIORITY]
    ordinary = [c for c in eligible if c.rule.priority < DISPLACE_PRIORITY]
    remaining = budget
    full: list[Chosen] = []
    pointers: list[Rule] = []
    for chosen in urgent + ordinary:
        size = len(chosen.rule.text)
        if size <= remaining:
            full.append(chosen)
            remaining -= size
        elif len(pointers) < GUIDANCE_POINTERS_MAX and remaining >= GUIDANCE_POINTER_COST:
            pointers.append(chosen.rule)
            remaining -= GUIDANCE_POINTER_COST
    return Selected(tuple(full), tuple(pointers))
