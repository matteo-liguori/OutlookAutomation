"""Exercise the agent-facing migration commands through their normal entry point."""

import hashlib
import json
from io import StringIO
from pathlib import Path

import pytest
from old_fixture import MemorySecrets

from chamberlain.importer import migrate
from chamberlain.surface import cli, migration


def test_cli_migrates_a_copy_and_switches_only_the_named_sandbox(
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prepare(old: Path, root: Path) -> migrate.MigrationStatus:
        return migrate.prepare_migration(old, root, secrets=secrets)

    monkeypatch.setattr(migration, "prepare_migration", prepare)

    def command(stage: str, *options: str, expected: int = 0) -> dict[str, object]:
        stream = StringIO()
        args = ["--data", str(data_root), "migrate", stage, str(old_root), *options]
        assert cli.main(args, stream) == expected, stream.getvalue()
        return json.loads(stream.getvalue())

    assert command("detect")
    assert command("status")["stage"] == "not_started"
    assert command("prepare")["stage"] == "imported"
    assert command("verify")["stage"] == "verified"
    sandbox = data_root / "import" / "sandbox" / "schedule.json"
    sandbox.parent.mkdir(parents=True)
    sandbox.write_text('{"active":"v1","schedules":[]}', encoding="utf-8")
    assert "error" in command("switch", "--sandbox-manifest", str(sandbox), expected=1)
    digest = hashlib.sha256(sandbox.read_bytes()).hexdigest()
    assert (
        command("switch", "--sandbox-manifest", str(sandbox), "--expected-sha256", digest)["stage"]
        == "switched"
    )
    assert command("status")["stage"] == "switched"
    assert json.loads(sandbox.read_bytes()) == {"active": "v2", "schedules": []}
