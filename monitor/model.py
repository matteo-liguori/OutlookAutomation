from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .profile import PROFILE
from automation_paths import data_root

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = data_root() / 'monitor/monitor.db'
LONDON = ZoneInfo("Europe/London")
BUDGET = 12000
OPEN = {"needs-user", "waiting", "watch"}
MAIL = list(PROFILE["mail"])
CAL = list(PROFILE["calendars"])


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp(value):
    if not value:
        return None
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=LONDON)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()
