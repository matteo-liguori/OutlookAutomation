"""Smoke tests for the real config-migration and release-note CLI routes."""

import json
from contextlib import closing
from io import StringIO
from pathlib import Path

from chamberlain.ports.clock import ManualClock
from chamberlain.store.db import DB_NAME, connect
from chamberlain.store.migrate import BASELINE_VERSION, discover, migrate
from chamberlain.surface.cli import main


def test_config_migrate_previews_unless_apply_is_explicit(
    data_root: Path, clock: ManualClock
) -> None:
    data_root.mkdir()
    database = data_root / DB_NAME
    migrations = discover()
    with closing(connect(database)) as store:
        migrate(store, clock, backup_dir=None, migrations=migrations[:1])
    before = database.read_bytes()
    command = ["--data", str(data_root), "config", "migrate"]

    preview = StringIO()
    assert main(command, preview) == 0
    assert json.loads(preview.getvalue()) == {
        "mode": "preview",
        "profile": None,
        "store_version": BASELINE_VERSION,
        "pending": [280, 290],
        "applied": [],
        "backup": None,
    }
    assert database.read_bytes() == before
    assert list(data_root.iterdir()) == [database]

    apply = StringIO()
    assert main([*command, "--apply"], apply) == 0
    result = json.loads(apply.getvalue())
    assert result["mode"] == "apply"
    assert result["applied"] == [280, 290]
    assert (Path(result["backup"]) / "pre-migration-0280.sqlite3").is_file()
