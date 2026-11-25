"""The owner's private instructions, standing facts and keyword watches, from the old harness.

Contract: docs/contracts/06-memory.md (M-41) and 07-attention.md (A-28). They go to the private
file `config/owner-instructions.yaml` in the data root, word for word. A topic is added once: one
that is already in the file (the owner or the agent wrote it) is never replaced, and one the owner
removed after an import is never brought back (the `imported_items` ledger).

The old `customisations.yaml` is prefilled with the old harness's shipped instruction texts, which
describe the old commands. Only a topic that differs from the shipped text recorded in the old
`.state/defaults.json` is the owner's own, so only those are carried.
"""

import sqlite3
from pathlib import Path
from typing import Any, cast

import yaml

from chamberlain.attention.owner_rules import default_where, path_of
from chamberlain.importer.old import OldInstallation
from chamberlain.importer.report import ImportReport
from chamberlain.memory import repo as memory

KIND = "instruction"
FACTS = "standing-facts"
WATCHES = "keyword-watches"


class _Literal(yaml.SafeDumper):
    pass


def _literal(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)  # pyright: ignore[reportUnknownMemberType]


_Literal.add_representer(str, _literal)


def _layers(old: OldInstallation) -> dict[str, str]:
    topics: dict[str, str] = {}
    for layer in (old.profile.get("instructions"), old.customisations.get("instructions")):
        if isinstance(layer, dict):
            for topic, text in cast(dict[Any, Any], layer).items():
                if isinstance(topic, str) and isinstance(text, str) and text.strip():
                    topics[topic] = text.strip()
    return topics


def collect(old: OldInstallation, report: ImportReport) -> dict[str, str]:
    """Topic to text: the owner's own instruction topics, then standing facts and watches."""
    found: dict[str, str] = {}
    baseline = old.default_instructions
    for topic, text in sorted(_layers(old).items()):
        if baseline is not None and baseline.get(topic, "").strip() == text:
            report.old_default_instructions.append(topic)
            continue
        found[topic] = text
    if baseline is None and found:
        report.warnings.append(
            "The old folder has no record of its shipped instruction texts, so every instruction "
            "topic was carried. Delete the ones that only describe the old commands from "
            "config/owner-instructions.yaml."
        )
    facts = [c for c in old.context if c.kind != "keyword-watch"]
    watches = [c for c in old.context if c.kind == "keyword-watch" and c.enabled and c.terms]
    if facts:
        found[FACTS] = "\n".join(
            f"- {c.entity}: {c.text}" if c.entity else f"- {c.text}" for c in facts
        )
    if watches:
        found[WATCHES] = "\n".join(
            f"- If a message mentions {' or '.join(c.terms)}: {c.text}" for c in watches
        )
    return found


def _existing(path: Path) -> dict[str, Any] | None:
    """The file's topics (empty when there is no file); None when it cannot be read."""
    if not path.is_file():
        return {}
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError):
        return None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        return None
    topics = cast(dict[str, Any], loaded).get("topics", {})
    return cast(dict[str, Any], topics) if isinstance(topics, dict) else None


def carry(
    db: sqlite3.Connection, old: OldInstallation, data_root: Path, now: str, report: ImportReport
) -> None:
    wanted = collect(old, report)
    path = path_of(data_root)
    topics = _existing(path)
    if topics is None:
        if wanted:
            report.warnings.append(
                "config/owner-instructions.yaml cannot be read, so no instruction was carried."
            )
        return
    added: dict[str, str] = {}
    for topic, text in wanted.items():
        if memory.imported_item_done(db, KIND, topic):
            report.count("owner_instructions", new=False)
            continue
        memory.record_imported_item(db, KIND, topic, "owner-instructions.yaml", now)
        if topic in topics:
            report.count("owner_instructions", new=False)
            continue
        topics[topic] = {"where": default_where(topic), "text": text}
        added[topic] = text
        report.count("owner_instructions", new=True)
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(".tmp")
        document = yaml.dump(  # pyright: ignore[reportUnknownMemberType]
            {"version": 1, "topics": topics},
            Dumper=_Literal,
            allow_unicode=True,
            sort_keys=True,
            width=100,
        )
        pending.write_text(document, encoding="utf-8", newline="\n")
        pending.replace(path)
    report.private_guidance = added
