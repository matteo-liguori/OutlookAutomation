# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""Guidance rules: docs/contracts/07-attention.md (A-11 to A-18) and 02-tools.md (T-1)."""

import json
import os
from pathlib import Path

import pytest
import yaml

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.attention.handlers import HANDLERS, HOOKS, guidance_checks
from chamberlain.attention.overrides import apply_overrides
from chamberlain.attention.rules import (
    FORBIDDEN_WORDS,
    Rule,
    check_trigger,
    content_problems,
    load_rules,
    rule_problems,
)
from chamberlain.limits import GUIDANCE_TOTAL_CHARS, RULE_MAX_CHARS, STANDING_PROMPT_MAX
from chamberlain.schema.registry import STANDING_PROMPT

RULES = load_rules()
LOCK = Path(__file__).with_name("rules.lock.json")
GUIDANCE_DIR = Path(__file__).resolve().parents[2] / "src" / "chamberlain" / "guidance"


def test_a12_exactly_48_guidance_rules_are_shipped() -> None:
    assert len(RULES) == 48
    assert len(list(GUIDANCE_DIR.glob("*.yaml"))) == 48


def test_t1_a12_the_standing_prompt_is_short_and_3_related_checks() -> None:
    # t1_the_standing_prompt_is_short
    assert len(STANDING_PROMPT) < STANDING_PROMPT_MAX

    # a12_every_rule_is_within_its_own_and_the_global_size
    for rule in RULES:
        assert len(rule.text) <= rule.max_chars <= RULE_MAX_CHARS, rule.id

    # a12_all_rules_together_are_within_the_total
    assert sum(len(r.text) for r in RULES) <= GUIDANCE_TOTAL_CHARS

    # a12_the_total_leaves_room_for_growth_and_overrides
    assert sum(len(r.text) for r in RULES) < GUIDANCE_TOTAL_CHARS * 0.85


def test_a13_a11_a14_at_least_ninety_percent_of_rules_have_a_trigger_and_3_related_checks() -> None:
    # a13_at_least_ninety_percent_of_rules_have_a_trigger
    triggered = [r for r in RULES if r.trigger is not None]
    assert len(triggered) * 10 >= len(RULES) * 9

    # a13_every_trigger_can_match_something
    problems = [f"{r.id}: {p}" for r in RULES for p in check_trigger(r)]
    assert problems == []

    # a13_the_checker_rejects_triggers_that_can_never_match
    base = {
        "id": "x.rule",
        "version": 1,
        "title": "t",
        "text": "Judgment.",
        "max_chars": 50,
        "once_per": "session",
        "priority": 10,
    }
    for when in (
        {"event": "operation.nope"},
        {"event": "operation.uncertain", "colour": "red"},
        {"event": "operation.uncertain", "intent_kind": "mail.fax"},
        {"tool": "start", "purpose": "nightly"},
        {"tool": "telepathy"},
        {"error": "no.such_code"},
        {"result": "coverage.complete"},
        {"event": "operation.uncertain", "tool": "start"},
    ):
        rule = Rule.model_validate({**base, "when": when})
        assert check_trigger(rule), when

    # a11_a14_no_rule_has_syntax_field_names_or_forbidden_words
    problems = [f"{r.id}: {p}" for r in RULES for p in content_problems(r.text)]
    assert problems == []
    assert rule_problems(RULES) == []


def test_a11_a14_a12_the_content_check_catches_syntax_and_old_and_3_related_checks() -> None:
    # a11_the_content_check_catches_syntax_and_old_vocabulary_and_passes_plain_text
    for text in [
        "Call find with {scope} set.",
        "Use `find` then stop.",
        "Run it with --force now.",
        "Set reason: because.",
        "Open the thread first.",
        "Check the case file.",
        "Ask for approval.",
        "Use Thunderbird for it.",
        "Run the monitor script.",
        "See the snapshot.",
    ]:
        assert content_problems(text), text
    assert content_problems("Tell the user what is known and stay in the case of doubt.") == [
        "uses the word case"
    ]
    assert content_problems("Tell the user what is known.") == []

    # a14_rules_name_no_forbidden_word_even_inside_titles
    for rule in RULES:
        for word in FORBIDDEN_WORDS:
            assert word not in rule.title.lower().split(), rule.id

    # a12_rule_files_match_their_ids_and_are_unique
    names = sorted(p.stem for p in GUIDANCE_DIR.glob("*.yaml"))
    assert names == [r.id for r in RULES]
    assert len(set(names)) == len(names)

    # a12_rule_titles_fit_the_index_and_are_distinct
    assert all(len(r.title) <= 60 for r in RULES)
    assert len({r.title for r in RULES}) == len(RULES)


def test_a12_a18_every_rule_file_has_only_the_known_keys_and_3_related_checks(
    data_root: Path,
) -> None:
    # a12_every_rule_file_has_only_the_known_keys
    allowed = {"id", "version", "title", "when", "text", "max_chars", "once_per", "priority"}
    for path in GUIDANCE_DIR.glob("*.yaml"):
        assert set(yaml.safe_load(path.read_text(encoding="utf-8"))) == allowed, path.name

    # a12_a_changed_rule_must_carry_a_raised_version
    current = {r.id: {"version": r.version, "hash": r.content_hash} for r in RULES}
    if os.environ.get("UPDATE_GUIDANCE_LOCK"):
        LOCK.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    locked = json.loads(LOCK.read_text(encoding="utf-8"))
    for rule_id, now in current.items():
        before = locked.get(rule_id)
        if before is None:
            continue
        if now["hash"] != before["hash"]:
            assert now["version"] > before["version"], f"{rule_id}: text or trigger changed"
        else:
            assert now["version"] == before["version"], f"{rule_id}: version moved, text did not"

    # a12_the_lock_lists_every_rule
    locked = json.loads(LOCK.read_text(encoding="utf-8"))
    assert sorted(locked) == [r.id for r in RULES]

    # a18_append_replace_and_disable_each_change_one_rule
    put_patch(
        data_root,
        "a",
        {"id": "do.once", "base_version": 2, "op": "append", "text": "Also tell me."},
    )
    put_patch(
        data_root,
        "b",
        {"id": "keep.owner", "base_version": 2, "op": "replace", "text": "Own words."},
    )
    put_patch(data_root, "c", {"id": "storage.orphans", "base_version": 1, "op": "disable"})
    applied = apply_overrides(RULES, data_root)
    assert (text_of(applied.rules, "do.once") or "").endswith("Also tell me.")
    assert text_of(applied.rules, "keep.owner") == "Own words."
    assert text_of(applied.rules, "storage.orphans") is None
    assert len(applied.rules) == len(RULES) - 1
    assert text_of(applied.rules, "card.upkeep") == text_of(RULES, "card.upkeep")
    assert applied.problems == ()
    assert applied.stale == ()


# --- overrides (A-18) ---------------------------------------------------------


def put_patch(data_root: Path, name: str, body: dict[str, object]) -> None:
    folder = data_root / "config" / "guidance"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def text_of(rules: tuple[Rule, ...], rule_id: str) -> str | None:
    return next((r.text for r in rules if r.id == rule_id), None)


def test_a18_a_patch_for_an_older_version_still_applies_and_and_2_related_checks(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, ctx: Context
) -> None:
    # a18_a_patch_for_an_older_version_still_applies_and_is_reported
    bumped = tuple(r.model_copy(update={"version": 3}) if r.id == "do.once" else r for r in RULES)
    put_patch(
        data_root, "a", {"id": "do.once", "base_version": 2, "op": "replace", "text": "Mine."}
    )
    applied = apply_overrides(bumped, data_root)
    assert text_of(applied.rules, "do.once") == "Mine."
    assert applied.stale == ("do.once",)

    # a18_a_patched_text_obeys_the_same_rules
    put_patch(
        data_root, "a", {"id": "do.once", "base_version": 2, "op": "replace", "text": "Run {it}."}
    )
    put_patch(
        data_root, "b", {"id": "keep.owner", "base_version": 2, "op": "append", "text": "x" * 700}
    )
    put_patch(data_root, "c", {"id": "no.such.rule", "base_version": 1, "op": "disable"})
    (data_root / "config" / "guidance" / "d.yaml").write_text("not: [a patch", encoding="utf-8")
    applied = apply_overrides(RULES, data_root)
    assert text_of(applied.rules, "do.once") == text_of(RULES, "do.once")
    assert text_of(applied.rules, "keep.owner") == text_of(RULES, "keep.owner")
    assert len(applied.problems) == 4

    # a18_overrides_reach_the_agent_and_doctor_reports_them
    put_patch(
        data_root, "a", {"id": "do.once", "base_version": 2, "op": "replace", "text": "Mine."}
    )
    app = App(ctx, HANDLERS, HOOKS)
    reply = app.call("guide", {"id": "do.once"})
    assert reply.data.rules[0].text == "Mine."  # type: ignore[union-attr]
    put_patch(
        data_root, "b", {"id": "keep.owner", "base_version": 2, "op": "replace", "text": "{bad}"}
    )
    status = {c.id: c.status for c in guidance_checks(ctx)}
    assert status["guidance.overrides"] == "warn"
