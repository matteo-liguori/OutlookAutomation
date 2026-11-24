"""Private overrides: a patch to one rule, never a wholesale copy.

Contract: docs/contracts/07-attention.md (A-18). Patch files live in
`<data root>/config/guidance/`. A patch whose `base_version` is behind the shared
rule still applies; `doctor` reports it for re-checking.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, ValidationError, model_validator

from chamberlain.attention.rules import Rule, content_problems
from chamberlain.limits import RULE_MAX_CHARS
from chamberlain.schema.base import Model

log = logging.getLogger("chamberlain.attention")


class Patch(Model):
    id: str
    base_version: int = Field(ge=1)
    op: Literal["append", "replace", "disable"]
    text: str | None = None

    @model_validator(mode="after")
    def _text_for_op(self) -> Self:
        if self.op == "disable" and self.text is not None:
            raise ValueError("disable takes no text")
        if self.op != "disable" and not (self.text and self.text.strip()):
            raise ValueError(f"{self.op} needs text")
        return self


@dataclass(frozen=True)
class Applied:
    rules: tuple[Rule, ...]
    stale: tuple[str, ...] = ()
    """Rule ids whose patch was written against an older version."""
    problems: tuple[str, ...] = field(default_factory=tuple)
    """Patches that were ignored, and why."""


def _read(directory: Path) -> tuple[list[Patch], list[str]]:
    patches: list[Patch] = []
    problems: list[str] = []
    if not directory.is_dir():
        return patches, problems
    for path in sorted(directory.glob("*.yaml")):
        try:
            patches.append(Patch.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))))
        except (OSError, yaml.YAMLError, ValidationError):
            problems.append(f"{path.name}: not a valid patch")
    return patches, problems


def apply_overrides(rules: tuple[Rule, ...], data_root: Path) -> Applied:
    """Apply the owner's patches. A bad patch is skipped and reported, never fatal."""
    patches, problems = _read(data_root / "config" / "guidance")
    by_id = {rule.id: rule for rule in rules}
    stale: list[str] = []
    seen: set[str] = set()
    for patch in patches:
        base = by_id.get(patch.id)
        if base is None or patch.id in seen:
            problems.append(f"{patch.id}: no such rule, or patched twice")
            continue
        seen.add(patch.id)
        if patch.base_version < base.version:
            stale.append(patch.id)
        if patch.op == "disable":
            del by_id[patch.id]
            continue
        text = (patch.text or "").strip()
        new_text = f"{base.text} {text}" if patch.op == "append" else text
        bad = content_problems(new_text)
        if len(new_text) > RULE_MAX_CHARS:
            bad.append("over the rule size limit")
        if bad:
            problems.append(f"{patch.id}: {'; '.join(bad)}")
            continue
        by_id[patch.id] = base.model_copy(
            update={"text": new_text, "max_chars": max(base.max_chars, len(new_text))}
        )
    return Applied(tuple(sorted(by_id.values(), key=lambda r: r.id)), tuple(stale), tuple(problems))
