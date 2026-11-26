"""Archived Step 3 migration chain for historical tests; never installed as runtime code."""

import json
from pathlib import Path

from chamberlain.store.migrate import Migration


def legacy_migrations() -> list[Migration]:
    archive = json.loads(
        (Path(__file__).parent / "legacy_migrations.json").read_text(encoding="utf-8")
    )
    return [Migration(int(name[:4]), name[5:-4], sql) for name, sql in sorted(archive.items())]
