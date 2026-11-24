"""The improvisation log, the review round and the Claude file guard (docs/contracts M-40)."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chamberlain.app.wiring import build_app
from chamberlain.ports.clock import ManualClock
from chamberlain.review import claude_guard, findings, normalise
from chamberlain.review.handlers import access_checks
from chamberlain.review.homes import AgentHomes
from chamberlain.review.rounds import RoundRule
from chamberlain.review.scan import ReviewSettings, scan
from chamberlain.schema.errors import ChamberlainError

SECRETS = ("TOPSECRET", "alice@example.org", "Quarterly", "hunter2")
NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def _lines(path: Path, *objects: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for obj in objects:
            handle.write(json.dumps(obj) + "\n")


def _claude(cwd: str, *commands: str, tool: str = "Bash") -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = [
        {
            "type": "user",
            "cwd": cwd,
            "message": {"content": "Mail from alice@example.org: TOPSECRET"},
        }
    ]
    for number, command in enumerate(commands):
        use: dict[str, Any] = (
            {"command": command} if tool == "Bash" else {"file_path": "C:/secret/TOPSECRET"}
        )
        block: dict[str, Any] = {
            "type": "tool_use",
            "id": f"{cwd}-{command}-{number}-{tool}",
            "name": tool,
            "input": use,
        }
        lines.append(
            {
                "type": "assistant",
                "cwd": cwd,
                "timestamp": "2026-03-01T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "TOPSECRET"}, block]},
            }
        )
        lines.append(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "hunter2 Quarterly"}]},
            }
        )
    return lines


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        self.homes = AgentHomes(root / "home" / ".claude", root / "home" / ".codex")
        self.settings = ReviewSettings(
            self.homes, self.checkout, RoundRule(new_patterns=5, min_days=3, max_days=21)
        )
        self.clock = ManualClock(NOW)
        self.app = build_app(root / "data", clock=self.clock, review=self.settings)

    def claude_file(self, name: str) -> Path:
        return self.homes.claude_transcripts / "project" / f"{name}.jsonl"

    def dump(self) -> str:
        tables = ("improv_patterns", "improv_files", "review_rounds", "review_findings")
        rows = [
            tuple(row)
            for table in tables
            for row in self.app.context.db.execute(f"SELECT * FROM {table}")
        ]
        return json.dumps(rows)

    def patterns(self) -> dict[tuple[str, str, str], int]:
        sql = "SELECT agent, tool, pattern, total FROM improv_patterns"
        return {(r[0], r[1], r[2]): r[3] for r in self.app.context.db.execute(sql)}

    def call(self, tool: str, request: dict[str, Any]) -> dict[str, Any]:
        return self.app.call(tool, request).model_dump(mode="json")


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.app.close()


def test_the_log_keeps_tool_names_and_short_summaries_only(rig: Rig) -> None:
    inside = str(rig.checkout / "src")
    _lines(
        rig.claude_file("a"),
        *_claude(
            inside,
            "git -C C:\\Users\\me\\TOPSECRET status && cd x",
            "curl https://example.com/?token=hunter2 | jq .",
            'powershell -NoProfile -Command "Get-Content C:\\mail\\Quarterly.txt; rg alice@x.org"',
            'chamberlain find --json \'{"query": "TOPSECRET"}\'',
        ),
        *_claude(inside, "", tool="Read"),
    )
    # A session elsewhere that never touches Chamberlain is not counted.
    _lines(rig.claude_file("other"), *_claude("C:/elsewhere", "ls TOPSECRET"))
    # A session elsewhere that called a Chamberlain tool is counted, earlier commands included.
    _lines(rig.claude_file("calls"), *_claude("C:/elsewhere", "npm run build"))
    _lines(rig.claude_file("calls"), *_claude("C:/elsewhere", "x", tool="mcp__chamberlain__start"))
    codex = rig.homes.codex_transcripts / "2026" / "rollout.jsonl"
    command = json.dumps({"command": "git status; sqlite3 data.db 'select hunter2'"})
    _lines(
        codex,
        {"type": "session_meta", "payload": {"cwd": inside}},
        {
            "type": "response_item",
            "timestamp": "2026-03-01T11:00:00Z",
            "payload": {"type": "function_call", "name": "shell_command", "arguments": command},
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": "TOPSECRET"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'x({cmd: "rg TOPSECRET"})',
            },
        },
    )
    ctx = rig.app.context

    assert scan(ctx, rig.settings) > 0
    first = rig.patterns()
    assert first[("claude", "Bash", "git status")] == 1
    assert first[("claude", "Bash", "curl")] == 1
    assert first[("claude", "Bash", "get-content")] == 1
    assert first[("claude", "Read", "")] == 1
    assert first[("claude", "Bash", "npm run")] == 1
    assert first[("codex", "shell_command", "sqlite3")] == 1
    assert first[("codex", "apply_patch", "")] == 1
    assert ("claude", "Bash", "ls") not in first
    assert not any(tool.startswith("mcp__chamberlain") for _, tool, _ in first)
    assert not any("chamberlain" in pattern for _, _, pattern in first)
    for secret in SECRETS:
        assert secret.lower() not in rig.dump().lower()

    scan(ctx, rig.settings)
    assert rig.patterns() == first  # a rescan counts nothing twice

    _lines(rig.claude_file("a"), *_claude(inside, "git status")[1:])
    scan(ctx, rig.settings)
    assert rig.patterns()[("claude", "Bash", "git status")] == 2
    assert normalise.summarise("cd x && ./deploy-to-bob.sh --now").patterns == ("(script)",)


def test_a_review_round_reaches_the_agent_and_only_worrying_reaches_the_owner(rig: Rig) -> None:
    inside = str(rig.checkout)
    _lines(rig.claude_file("a"), *_claude(inside, "git status", "git diff"))
    ctx = rig.app.context
    started = rig.call("start", {"agent": "t", "purpose": "interactive"})
    sid = started["data"]["session"]
    assert not [w for w in started["data"]["work"] if w["kind"] == "harness.review"]

    rig.clock.advance(hours=480)
    sid = rig.call("start", {"agent": "t", "purpose": "interactive"})["data"]["session"]
    assert ctx.db.execute("SELECT count(*) FROM review_rounds").fetchone()[0] == 0  # too early

    rig.clock.advance(hours=48)
    sid = rig.call("start", {"agent": "t", "purpose": "interactive"})["data"]["session"]
    rig.clock.advance(minutes=2)
    work = rig.call("next", {"session": sid, "limit": 10})["data"]["work"]
    item = next(w for w in work if w["kind"] == "harness.review")
    assert item["next"]["tool"] == "open"
    body = rig.call("open", {"session": sid, **item["next"]["args"]})
    text = body["data"]["content"]["text"]
    assert "git status" in text
    assert "NEW" in text

    def keep(owner: str, text: str) -> dict[str, Any]:
        request = {"session": sid, "kind": "finding", "owner": owner, "reason": "review"}
        return rig.call("keep", {**request, "text": text})

    event = item["event"]
    assert keep(event, "worrying | It read a private folder with a shell.")["ok"]
    assert keep(event, "idea | A find option for stored text would replace most of these.")["ok"]
    assert not keep(event, "sad | no")["ok"]
    assert not keep("card_x", "idea | x")["ok"]

    lines, ids = findings.report_lines(ctx)
    assert "private folder" in lines
    assert "find option" not in lines
    findings.mark_reported(ctx, ids)
    assert findings.report_lines(ctx) == ("", [])

    rig.call("next", {"session": sid, "handled": [event], "limit": 10})
    rig.clock.advance(minutes=2)
    rig.call("doctor", {"session": sid})
    rig.clock.advance(minutes=2)
    rig.call("doctor", {"session": sid})
    assert [r[0] for r in ctx.db.execute("SELECT state FROM review_rounds")] == ["done"]

    rig.clock.advance(hours=720)
    sid = rig.call("start", {"agent": "t", "purpose": "interactive"})["data"]["session"]
    assert ctx.db.execute("SELECT count(*) FROM review_rounds").fetchone()[0] == 1  # nothing new

    # A burst of new workarounds brings the next review forward: days, not the long wait.
    burst = ("git log", "npm run", "pip install", "docker ps", "curl", "node")
    _lines(rig.claude_file("b"), *_claude(inside, *burst))
    rig.clock.advance(hours=25)
    rig.call("start", {"agent": "t", "purpose": "interactive"})
    assert ctx.db.execute("SELECT count(*) FROM review_rounds").fetchone()[0] == 2


def test_the_claude_guard_adds_once_keeps_other_rules_and_is_checked_by_doctor(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = rig.root / "data"
    settings = rig.homes.claude / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"model": "x", "permissions": {"deny": ["Bash(rm *)"], "allow": ["Read"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHAMBERLAIN_AGENT_HOME", str(rig.root / "home"))
    ctx = rig.app.context
    assert access_checks(ctx)[0].status == "warn"

    claude_guard.add(data)
    claude_guard.add(data)
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["model"] == "x"
    assert saved["permissions"]["allow"] == ["Read"]
    deny = saved["permissions"]["deny"]
    assert deny[0] == "Bash(rm *)"
    assert len(deny) == 3
    assert all(r.startswith(("Read(//", "Edit(//")) and r.endswith("/**)") for r in deny[1:])
    assert access_checks(ctx)[0].status == "ok"

    saved["permissions"]["deny"].remove(deny[1])
    settings.write_text(json.dumps(saved), encoding="utf-8")
    assert access_checks(ctx)[0].status == "warn"  # a missing rule is noticed

    claude_guard.add(data)
    assert claude_guard.remove(data)
    remaining = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert remaining == ["Bash(rm *)"]
    assert not claude_guard.remove(data)

    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(ChamberlainError):
        claude_guard.add(data)
    assert settings.read_text(encoding="utf-8") == "{not json"
