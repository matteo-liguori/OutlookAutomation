"""Pre-R1 approval bytes and handle deduplication survive the batch representation change."""

import json

import pytest

from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.grant_rules import intent_hash
from chamberlain.domain.intents import MailTrash
from chamberlain.domain.operations import OperationState
from chamberlain.store.migrate import migrate
from chamberlain.writes import repo
from support.write_rig import MAIL, WriteRig


@pytest.mark.parametrize("identity_matches", [True, False])
def test_migrated_approved_trash_keeps_identity_and_never_dispatches_twice(
    rig: WriteRig, identity_matches: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = rig.seed_message("<old@local>")
    intent = MailTrash(source=MAIL, messages=(h,))
    engine = rig.engine()
    old = {
        "kind": "mail.trash",
        "source": MAIL,
        "from_folder": "f-inbox",
        "message": h,
        "internet_message_id": "<old@local>" if identity_matches else "<other@local>",
    }
    encoded = json.dumps(old, separators=(",", ":"), sort_keys=True)
    old_hash = sha256_text(encoded)
    with monkeypatch.context() as patch:
        patch.setattr("chamberlain.writes.core.intent_hash", lambda _: old_hash)
        proposal = engine.propose_intent(intent)
        approved = rig.agent_approves(proposal, "Please trash this message.")
        assert approved.operation
        op = approved.operation
    rig.db.execute("UPDATE operations SET intent_json=? WHERE handle=?", (encoded, op.handle))
    rig.db.execute("DELETE FROM schema_migrations WHERE version=280")
    assert migrate(rig.db, rig.clock, backup_dir=None) == [280]
    converted = engine.get(op.handle)
    assert converted.refs()["_original_move_intent"] == encoded
    assert converted.intent_sha256 == old_hash
    assert rig.grant_for(converted).content_sha256 == old_hash
    assert repo.live_by_hash(rig.db, MAIL, intent_hash(converted.intent())) == [converted]
    if not identity_matches:
        from chamberlain.schema.errors import ChamberlainError

        with pytest.raises(ChamberlainError, match="identity differs"):
            engine.do(op.handle)
        assert engine.get(op.handle).state is OperationState.FAILED_CLEAN
        assert not rig.fake.world.dispatch_log
        return
    assert engine.do(op.handle).state is OperationState.CONFIRMED
    assert engine.do(op.handle).state is OperationState.CONFIRMED
    assert len(rig.fake.world.dispatch_log) == 1
