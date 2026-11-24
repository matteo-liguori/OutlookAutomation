# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""Golden replies for `start`, `next`, `guide` and `doctor` over the fake provider.

The clock is a ManualClock, so every timestamp is stable. Random handles are
numbered in order of appearance. Set CHAMBERLAIN_UPDATE_GOLDEN=1 to rewrite the
files after an intended change.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.app.doctor import make_doctor
from chamberlain.attention.handlers import DOCTOR_CHECKS, HANDLERS, HOOKS
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import Reply

sys.path.insert(0, str(Path(__file__).parent.parent / "memory"))
from memory_helpers import normalise  # pyright: ignore[reportMissingImports]

GOLDEN = Path(__file__).parent / "golden"


def golden(name: str, reply: Reply[Model]) -> None:
    text = normalise(json.dumps(reply.model_dump(mode="json", exclude_none=True), indent=2))
    path = GOLDEN / f"{name}.json"
    if os.environ.get("CHAMBERLAIN_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == text + "\n", f"{name} differs from its golden file"


def test_start_next_and_guide_golden(app: App, seed: Any) -> None:
    seed.grant()
    seed.operation()
    start = app.call("start", {"agent": "golden"})
    assert start.ok
    golden("start", start)
    session = str(start.data.session)
    events = [w.event for w in start.data.work]
    nxt = app.call("next", {"session": session, "handled": events[:1]})
    assert nxt.ok
    golden("next", nxt)
    guide = app.call("guide", {"session": session, "id": "send.uncertain"})
    assert guide.ok
    golden("guide", guide)
    index = app.call("guide", {})
    assert index.ok
    golden("guide_index", index)


def test_doctor_golden(ctx: Context) -> None:
    app = App(ctx, {**HANDLERS, ToolName.DOCTOR: make_doctor(DOCTOR_CHECKS)}, HOOKS)
    plain = app.call("doctor", {})
    assert plain.ok
    golden("doctor", plain)
    metrics = app.call("doctor", {"metrics": True})
    assert metrics.ok
    golden("doctor_metrics", metrics)
