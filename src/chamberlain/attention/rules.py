"""Guidance rules: the file format, the loader, the validator and the content hash.

Contract: docs/contracts/07-attention.md (Guidance rules, A-11 to A-14).
Rules live as `<id>.yaml` files in `chamberlain/guidance/`. A rule is judgment
only: no command syntax, no field names, only the nouns of 01-domain.md.
"""

import json
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Literal, Self, cast

import yaml
from pydantic import Field, TypeAdapter, ValidationError, model_validator
from pydantic.fields import FieldInfo

from chamberlain.attention.models import EventAttrs
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.events import EventKind
from chamberlain.limits import GUIDANCE_TOTAL_CHARS, RULE_MAX_CHARS
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.errors import ChamberlainError, ErrorCategory, ErrorCode
from chamberlain.schema.registry import BY_NAME

OncePer = Literal["session", "card", "operation", "event", "always"]
_ID = r"^[a-z0-9_.]{3,40}$"
_RESULT = re.compile(r"^([a-z_][a-z0-9_.]*)\s*(==|!=|>=|<=|>|<)\s*(.+)$")

FORBIDDEN_WORDS = (
    "outlook",
    "monitor",
    "pilot",
    "case",
    "thread",
    "approval",
    "entryid",
    "snapshot",
    "thunderbird",
    "google",
    "caldav",
    "icloud",
    "imap",
    "davmail",
    "owl",
)
"""D-1 words and connector names that must not reach an agent (A-14)."""

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


TriggerKind = Literal["event", "tool", "result", "error"]


@dataclass(frozen=True)
class Trigger:
    kind: TriggerKind
    value: str
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    """Equality filters; several listed values mean any of them."""


class Rule(Model):
    id: str = Field(pattern=_ID)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=60)
    when: dict[str, Any]
    text: str = Field(min_length=1)
    max_chars: int = Field(ge=1, le=RULE_MAX_CHARS)
    once_per: OncePer
    priority: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _fits(self) -> Self:
        if len(self.text) > self.max_chars:
            raise ValueError(f"text is {len(self.text)} characters, over max_chars")
        return self

    @property
    def trigger(self) -> Trigger | None:
        """The trigger, or None for a rule only fetched with `guide` (A-13)."""
        return parse_when(self.when)

    @property
    def content_hash(self) -> str:
        return sha256_text(json.dumps([self.text, self.when], sort_keys=True))


def _values(value: object) -> tuple[str, ...]:
    items: list[object] = list(cast(list[object], value)) if isinstance(value, list) else [value]
    return tuple(str(item) for item in items)


def parse_when(when: dict[str, Any]) -> Trigger | None:
    """Split `when` into its one trigger and its filters; empty means guide-only."""
    if not when:
        return None
    keys = [k for k in ("event", "tool", "result", "error") if k in when]
    if len(keys) != 1:
        raise ValueError("when needs exactly one trigger: event, tool, result or error")
    key = keys[0]
    filters = tuple(sorted((name, _values(value)) for name, value in when.items() if name != key))
    return Trigger(cast(TriggerKind, key), str(when[key]), filters)


def _request_fields(tool: str) -> set[str]:
    return set(BY_NAME[tool].request.model_fields)


def _accepts(field: FieldInfo, value: str) -> bool:
    if field.annotation is None:
        return True
    try:
        TypeAdapter(field.annotation).validate_python(value)
    except ValidationError:
        return False
    return True


def check_trigger(rule: Rule) -> list[str]:
    """Problems with the trigger: unknown names would never match (A-13)."""
    try:
        trigger = rule.trigger
    except ValueError as error:
        return [str(error)]
    if trigger is None:
        return []
    problems: list[str] = []
    names = {name for name, _ in trigger.filters}
    fields: dict[str, FieldInfo] = {}
    if trigger.kind == "event":
        fields = dict(EventAttrs.model_fields)
    elif trigger.kind == "tool" and trigger.value in BY_NAME:
        fields = dict(BY_NAME[trigger.value].request.model_fields)
    for name, values in trigger.filters:
        if name in fields:
            problems += [f"{name} cannot be {v}" for v in values if not _accepts(fields[name], v)]
    if trigger.kind == "event":
        if trigger.value not in {k.value for k in EventKind}:
            problems.append(f"unknown event kind {trigger.value}")
        problems += [f"unknown event filter {n}" for n in names - set(EventAttrs.model_fields)]
    elif trigger.kind == "tool":
        if trigger.value not in {t.value for t in ToolName}:
            problems.append(f"unknown tool {trigger.value}")
        else:
            problems += [
                f"unknown request field {n}" for n in names - _request_fields(trigger.value)
            ]
    else:
        if names:
            problems.append("result and error triggers take no filters")
        if trigger.kind == "result" and _RESULT.match(trigger.value) is None:
            problems.append("result needs a path, an operator and a value")
        if trigger.kind == "error":
            known = {c.value for c in ErrorCode} | {c.value for c in ErrorCategory}
            if trigger.value not in known:
                problems.append(f"unknown error {trigger.value}")
    return problems


def request_field_names() -> frozenset[str]:
    """Every request field name, which guidance must not show with a colon (A-11)."""
    names: set[str] = set()
    for spec in BY_NAME.values():
        names |= set(spec.request.model_fields)
    return frozenset(names)


def content_problems(text: str) -> list[str]:
    """Reasons a text breaks A-11 or A-14. Empty means it is clean."""
    problems: list[str] = []
    for token in ("{", "}", "`", "[", "]"):
        if token in text:
            problems.append(f"contains {token!r}")
    if re.search(r"(^|\s)--[a-z]", text):
        problems.append("contains a command flag")
    lowered = text.lower()
    for name in sorted(request_field_names()):
        if re.search(rf"\b{re.escape(name)}\s*:", lowered):
            problems.append(f"shows the field {name} with a colon")
    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{word}s?\b", lowered):
            problems.append(f"uses the word {word}")
    return problems


def result_matches(trigger: Trigger, reply: dict[str, Any]) -> bool:
    """Evaluate a `result:` trigger against the serialised reply (A-15 deterministic)."""
    match = _RESULT.match(trigger.value)
    if match is None:
        return False
    path, op, raw = match.groups()
    try:
        expected: Any = json.loads(raw)
    except ValueError:
        expected = raw.strip()
    current: Any = reply
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]  # type: ignore[reportUnknownVariableType]
    try:
        return bool(_OPS[op](current, expected))
    except TypeError:
        return False


def _load_one(text: str, name: str) -> Rule:
    loaded: object = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ChamberlainError(ErrorCode.CONFIG_INVALID, f"Guidance file {name} is not a mapping.")
    raw = cast(dict[str, Any], loaded)
    try:
        rule = Rule.model_validate({**raw, "text": str(raw.get("text", "")).strip()})
    except ValidationError as error:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID, f"Guidance file {name} is invalid.", detail=str(error)
        ) from error
    if f"{rule.id}.yaml" != name:
        raise ChamberlainError(ErrorCode.CONFIG_INVALID, f"Guidance file {name} has another id.")
    return rule


@cache
def load_rules() -> tuple[Rule, ...]:
    """The shared rules, sorted by id. Cached for the life of the process."""
    found: list[Rule] = []
    for entry in resources.files("chamberlain.guidance").iterdir():
        if entry.name.endswith(".yaml"):
            found.append(_load_one(entry.read_text(encoding="utf-8"), entry.name))
    return tuple(sorted(found, key=lambda r: r.id))


def rule_problems(rules: tuple[Rule, ...]) -> list[str]:
    """Every size, content and trigger problem across a rule set (A-11 to A-14)."""
    problems: list[str] = []
    total = sum(len(r.text) for r in rules)
    if total > GUIDANCE_TOTAL_CHARS:
        problems.append(f"all rules total {total} characters, over {GUIDANCE_TOTAL_CHARS}")
    guide_only = [r for r in rules if r.trigger is None]
    if rules and len(guide_only) * 10 > len(rules):
        problems.append(f"{len(guide_only)} of {len(rules)} rules have no trigger")
    for rule in rules:
        problems += [f"{rule.id}: {p}" for p in check_trigger(rule)]
        problems += [f"{rule.id}: {p}" for p in content_problems(rule.text)]
    return problems
