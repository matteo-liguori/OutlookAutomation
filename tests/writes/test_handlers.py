"""The writes handlers through `App.call`, with golden replies (02-tools.md)."""

import json
import os
import re
from pathlib import Path
from typing import Any

from chamberlain.app.core import App
from chamberlain.app.wiring import load_tracks
from chamberlain.connectors.fake import Fault
from chamberlain.schema.base import ToolName
from chamberlain.testing.write_rig import MAIL, SESSION, WriteRig
from chamberlain.writes.handlers import DOCTOR_CHECKS, HANDLERS, make_handlers

GOLDEN = Path(__file__).parent / "golden" / "handlers.json"
HANDLE = re.compile(r"\b(op|grant|evd)_[0-9a-f]{16}\b")
INTENT = {
    "kind": "mail.send",
    "source": MAIL,
    "to": ["friend@example.org"],
    "subject": "Dinner",
    "body": "Are you free on Friday?",
}


class Recorder:
    def __init__(self, rig: WriteRig) -> None:
        self.rig = rig
        self.app = App(rig.ctx, make_handlers(rig.policy, pause=rig._pause))
        self.names: dict[str, str] = {}
        self.log: list[dict[str, Any]] = []

    def call(self, label: str, tool: str, request: dict[str, Any]) -> dict[str, Any]:
        reply = self.app.call(tool, {"session": SESSION, **request})
        dumped: dict[str, Any] = json.loads(reply.model_dump_json(exclude_none=True))
        self.log.append({"call": label, "tool": tool, "reply": self._normalise(dumped)})
        return dumped

    def _normalise(self, value: Any) -> Any:
        text = json.dumps(value, sort_keys=True)

        def name(match: re.Match[str]) -> str:
            full = match.group(0)
            if full not in self.names:
                same = [n for n in self.names.values() if n.startswith(f"<{match.group(1)}")]
                self.names[full] = f"<{match.group(1)}{len(same) + 1}>"
            return self.names[full]

        return json.loads(HANDLE.sub(name, text))


def test_the_replies_of_propose_do_and_reconcile_match_the_golden_file(rig: WriteRig) -> None:
    rig.know("friend@example.org")
    rec = Recorder(rig)
    proposed = rec.call("propose a known send", "propose", {"intent": INTENT})
    op = proposed["data"]["operation"]
    evidence = rig.evidence.add(text="Yes, send it.", owner=op, session=SESSION)
    rec.call("attach approval", "propose", {"operation": op, "approval_evidence": evidence})
    rec.call("do", "do", {"operation": op})
    rec.call("do again", "do", {"operation": op})
    rec.call("propose the same again", "propose", {"intent": INTENT})

    other = {**INTENT, "subject": "Lunch"}
    second = rec.call("propose another", "propose", {"intent": other})
    second_op = second["data"]["operation"]
    second_evidence = rig.evidence.add(text="Yes.", owner=second_op, session=SESSION)
    rec.call(
        "approve another", "propose", {"operation": second_op, "approval_evidence": second_evidence}
    )
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    rec.call("do, outcome unknown", "do", {"operation": second_op})
    rec.call("do while uncertain", "do", {"operation": second_op})
    rec.call("reconcile", "reconcile", {"operation": second_op})

    stranger = {**INTENT, "to": ["stranger@example.org"]}
    third = rec.call("propose to a stranger", "propose", {"intent": stranger})
    third_op = third["data"]["operation"]
    third_evidence = rig.evidence.add(text="Yes.", owner=third_op, session=SESSION)
    rec.call(
        "approve a stranger",
        "propose",
        {"operation": third_op, "approval_evidence": third_evidence},
    )
    rec.call("do without permission", "do", {"operation": third_op})
    rec.call("do an unknown handle", "do", {"operation": "op_0123456789abcdef"})

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(rec.log, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == rec.log


def test_every_reply_fits_the_envelope_rules(rig: WriteRig) -> None:
    rig.know("friend@example.org")
    rec = Recorder(rig)
    proposed = rec.call("propose", "propose", {"intent": INTENT})
    assert proposed["ok"]
    assert proposed["data"]["preview"]["kind"] == "preview"
    assert proposed["data"]["preview"]["origin"] == proposed["data"]["operation"]
    for entry in rec.log:
        assert len(json.dumps(entry["reply"])) < 6000
        assert entry["reply"]["ok"] == (entry["reply"].get("error") is None)


def test_the_uncertain_error_never_claims_to_be_retryable(rig: WriteRig) -> None:
    rig.know("friend@example.org")
    rec = Recorder(rig)
    op = rec.call("propose", "propose", {"intent": INTENT})["data"]["operation"]
    evidence = rig.evidence.add(text="Yes.", owner=op, session=SESSION)
    rec.call("approve", "propose", {"operation": op, "approval_evidence": evidence})
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    reply = rec.call("do", "do", {"operation": op})
    assert reply["error"]["category"] == "uncertain"
    assert reply["error"]["may_have_happened"] is True
    assert reply["error"]["retryable"] is False
    assert op in reply["error"]["message"]


def test_the_module_fills_the_names_wiring_reads() -> None:
    assert set(HANDLERS) == {ToolName.PROPOSE, ToolName.DO, ToolName.RECONCILE}
    handlers, _ = load_tracks()
    assert {ToolName.PROPOSE, ToolName.DO, ToolName.RECONCILE} <= set(handlers)
    assert [group.value for group, _ in DOCTOR_CHECKS] == ["audit"]


def test_a_send_that_is_only_client_verified_is_called_submitted_never_delivered(
    rig: WriteRig,
) -> None:
    rig.know("friend@example.org")
    rec = Recorder(rig)
    op = rec.call("propose", "propose", {"intent": INTENT})["data"]["operation"]
    evidence = rig.evidence.add(text="Yes.", owner=op, session=SESSION)
    rec.call("approve", "propose", {"operation": op, "approval_evidence": evidence})
    rig.fake.arm(Fault.STALE, 10)
    done = rec.call("do", "do", {"operation": op})
    text = json.dumps(done).lower()
    assert "delivered" not in text
    assert done["data"]["operation"]["verification"] == {
        "client_verified": True,
        "server_verified": False,
    }
