"""`setup agent`: the Codex block and the Claude project file, with a fake server probe."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chamberlain.connections.agent_setup import (
    parse_initialize,
    replace_block,
    setup_claude,
    setup_codex,
)
from chamberlain.schema.errors import ChamberlainError

ROOT = Path("C:/data/chamberlain-data")
PYTHON = Path("C:/dev/chamberlain/.venv/Scripts/python.exe")
NOW = datetime(2026, 1, 5, 17, 0, tzinfo=UTC)
ARGS = 'args = ["-m", "chamberlain.surface.mcp_server", "--data", "C:/data/chamberlain-data"]'


class Probe:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> tuple[str, str]:
        self.commands.append(list(command))
        return "chamberlain", "1.0.0"


def codex(config: Path, *, apply: bool, probe: Probe) -> list[str]:
    return setup_codex(
        ROOT, apply=apply, config=config, python=PYTHON, probe=probe, now=lambda: NOW
    )


def test_codex_dry_run_shows_the_block_and_writes_nothing(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    probe = Probe()
    lines = codex(config, apply=False, probe=probe)
    assert "[mcp_servers.chamberlain]" in "\n".join(lines)
    assert not config.exists()
    assert probe.commands == []


def test_codex_apply_replaces_only_its_own_block_after_a_backup(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        'model = "x"\n\n[mcp_servers.other]\ncommand = "o"\n\n'
        '[mcp_servers.chamberlain]\ncommand = "old"\nargs = []\n\n'
        '[mcp_servers.chamberlain.env]\nA = "1"\n\n[projects.a]\ntrust = "yes"\n',
        encoding="utf-8",
    )
    probe = Probe()
    lines = codex(config, apply=True, probe=probe)
    text = config.read_text(encoding="utf-8")
    assert 'command = "old"' not in text
    assert "chamberlain.env" not in text
    assert text.count("[mcp_servers.chamberlain]") == 1
    for kept in ('model = "x"', "[mcp_servers.other]", '[projects.a]\ntrust = "yes"'):
        assert kept in text
    assert ARGS in text
    assert len(list(config.parent.glob("config.toml.bak-*"))) == 1
    assert probe.commands[0][0] == PYTHON.as_posix()
    assert lines[-1] == "Verified: the server answered as chamberlain 1.0.0."
    codex(config, apply=True, probe=probe)  # again: nothing to change, no second backup
    assert config.read_text(encoding="utf-8") == text
    assert len(list(config.parent.glob("config.toml.bak-*"))) == 1


def test_replace_block_appends_when_absent() -> None:
    assert replace_block("a = 1\n", "[x]\n") == "a = 1\n\n[x]\n"
    assert replace_block("", "[x]\n") == "[x]\n"


def test_claude_without_a_project_prints_the_command() -> None:
    lines = setup_claude(ROOT, apply=False, python=PYTHON)
    assert "claude mcp add chamberlain --" in "\n".join(lines)


def test_claude_project_file_is_merged_and_backed_up(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "o"}}}), encoding="utf-8"
    )
    probe = Probe()
    setup_claude(ROOT, apply=False, project=project, python=PYTHON, probe=probe)
    assert "chamberlain" not in (project / ".mcp.json").read_text(encoding="utf-8")
    lines = setup_claude(
        ROOT, apply=True, project=project, python=PYTHON, probe=probe, now=lambda: NOW
    )
    servers = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers["other"] == {"command": "o"}
    assert servers["chamberlain"]["args"][-2:] == ["--data", "C:/data/chamberlain-data"]
    assert len(list(project.glob(".mcp.json.bak-*"))) == 1
    assert lines[-1].startswith("Verified")


def test_parse_initialize_reads_name_and_version_or_fails() -> None:
    ok = json.dumps({"result": {"serverInfo": {"name": "chamberlain", "version": "1.2.3"}}})
    assert parse_initialize(ok) == ("chamberlain", "1.2.3")
    with pytest.raises(ChamberlainError):
        parse_initialize("")
