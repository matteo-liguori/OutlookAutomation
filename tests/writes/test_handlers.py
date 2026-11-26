"""The writes handlers through `App.call`, with golden replies (02-tools.md)."""

import json
from typing import Any

from chamberlain.app.core import App
from chamberlain.connectors.fake import Fault
from chamberlain.domain.grants import Requirement
from chamberlain.writes.handlers import make_handlers
from chamberlain.writes.models import WritePolicy
from support.write_rig import MAIL, SESSION, WriteRig, make_rig

INTENT = {
    "kind": "mail.send",
    "source": MAIL,
    "to": ["friend@example.org"],
    "subject": "Dinner",
    "body": "Are you free on Friday?",
}


def test_handlers_use_the_policy_on_the_current_context() -> None:
    rig = make_rig(policy=WritePolicy(send_known=Requirement.HUMAN_CONFIRMATION))
    try:
        rig.know("friend@example.org")
        response = App(rig.ctx, make_handlers()).call(
            "propose", {"session": SESSION, "intent": INTENT}
        )
        assert response.data is not None
        assert response.data.model_dump()["requirement"] == Requirement.HUMAN_CONFIRMATION
    finally:
        rig.close()


class Recorder:
    def __init__(self, rig: WriteRig) -> None:
        self.rig = rig
        self.app = App(rig.ctx, make_handlers(pause=rig._pause))
        self.log: list[dict[str, Any]] = []

    def call(self, label: str, tool: str, request: dict[str, Any]) -> dict[str, Any]:
        reply = self.app.call(tool, {"session": SESSION, **request})
        dumped: dict[str, Any] = json.loads(reply.model_dump_json(exclude_none=True))
        self.log.append({"call": label, "tool": tool, "reply": dumped})
        return dumped


def test_e1_e2_every_reply_fits_the_envelope_rules(rig: WriteRig) -> None:
    rig.know("friend@example.org")
    rec = Recorder(rig)
    proposed = rec.call("propose", "propose", {"intent": INTENT})
    assert proposed["ok"]
    assert proposed["data"]["preview"]["kind"] == "preview"
    assert proposed["data"]["preview"]["origin"] == proposed["data"]["operation"]
    for entry in rec.log:
        assert len(json.dumps(entry["reply"])) < 6000
        assert entry["reply"]["ok"] == (entry["reply"].get("error") is None)


def test_e11_e12_the_uncertain_error_never_claims_to_be_retryable(rig: WriteRig) -> None:
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


def test_w14_a_send_that_is_only_client_verified_is_called_submitted_never_delivered(
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
