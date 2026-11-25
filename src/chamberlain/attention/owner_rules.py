"""The owner's own rules: a private file in the data root, delivered as guidance.

Contract: docs/contracts/07-attention.md (A-28). `<data root>/config/owner-instructions.yaml` maps
a topic to the owner's text. Topics are grouped by where the agent needs them and each group
becomes one rule. This is the owner's own wording, so the checks that keep shared guidance free of
syntax and jargon (A-11, A-14) do not apply; the size rules of a reply still do (a group over
`RULE_MAX_CHARS` is only ever sent as a pointer, see `selection.select`).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from chamberlain.attention.rules import Rule

log = logging.getLogger("chamberlain.attention")

OWNER_FILE = Path("config") / "owner-instructions.yaml"
WHERE_WHEN: dict[str, dict[str, str]] = {
    "start": {"tool": "start"},
    "mail": {"event": "mail.new_unmatched"},
    "report": {"event": "report.due"},
    "calendar": {"tool": "find", "scope": "calendar"},
}
WHERE_TITLE = {
    "start": "Your own rules",
    "mail": "Your own rules for mail",
    "report": "Your own rules for the report",
    "calendar": "Your own rules for the calendar",
}
PRIORITY = 66
_BY_NAME = (
    ("report", "report"),
    ("calendar", "calendar"),
    ("filing", "mail"),
    ("mail", "mail"),
    ("triage", "mail"),
    ("watch", "mail"),
)
"""A topic whose name contains the first word is placed at the second (else `start`)."""


@dataclass(frozen=True)
class Topic:
    name: str
    text: str
    where: str


def default_where(topic: str) -> str:
    lowered = topic.lower()
    for word, where in _BY_NAME:
        if word in lowered:
            return where
    return "start"


def path_of(data_root: Path) -> Path:
    return data_root / OWNER_FILE


def read_topics(data_root: Path) -> tuple[list[Topic], list[str]]:
    """The topics in the file and the problems met. A bad file or entry is skipped, never fatal."""
    path = path_of(data_root)
    if not path.is_file():
        return [], []
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError):
        return [], [f"{OWNER_FILE.name}: cannot be read"]
    topics: Any = cast(dict[str, Any], loaded).get("topics") if isinstance(loaded, dict) else None
    if not isinstance(topics, dict):
        return [], [f"{OWNER_FILE.name}: has no topics"]
    found: list[Topic] = []
    problems: list[str] = []
    for name, entry in sorted(cast(dict[Any, Any], topics).items(), key=lambda kv: str(kv[0])):
        text: Any = cast(dict[str, Any], entry).get("text") if isinstance(entry, dict) else entry
        where: Any = cast(dict[str, Any], entry).get("where") if isinstance(entry, dict) else None
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{OWNER_FILE.name}: topic {name} has no text")
            continue
        if where is not None and where not in WHERE_WHEN:
            problems.append(f"{OWNER_FILE.name}: topic {name} has an unknown place")
            where = None
        found.append(Topic(str(name), text.strip(), where or default_where(str(name))))
    return found, problems


def owner_rules(data_root: Path) -> tuple[tuple[Rule, ...], list[str]]:
    """One rule per place that has topics, text kept whole."""
    topics, problems = read_topics(data_root)
    rules: list[Rule] = []
    for where, when in WHERE_WHEN.items():
        mine = [t for t in topics if t.where == where]
        if not mine:
            continue
        text = "\n\n".join(f"{t.name}: {t.text}" for t in mine)
        rules.append(
            Rule.model_construct(
                id=f"owner.{where}",
                version=1,
                title=WHERE_TITLE[where],
                when=dict(when),
                text=text,
                max_chars=len(text),
                once_per="session",
                priority=PRIORITY,
            )
        )
    return tuple(rules), problems
