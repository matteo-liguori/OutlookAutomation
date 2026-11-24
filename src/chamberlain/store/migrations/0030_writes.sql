-- 0030 writes track: dispatch lease (R5), unique reconcile tokens, tamper guards.
-- Contract: docs/contracts/04-writes.md, 05-grants.md (G-2, G-3, G-20), board R5.

ALTER TABLE operations ADD COLUMN lease_owner TEXT;
ALTER TABLE operations ADD COLUMN lease_expires_at TEXT;

-- A reconcile token names exactly one operation, so reconcile can never
-- confirm one operation by finding another's message.
CREATE UNIQUE INDEX operations_reconcile_token
    ON operations (reconcile_token) WHERE reconcile_token IS NOT NULL;

-- audit_log is append-only (G-20). Rows may be removed only up to the
-- checkpoint the pruning step records first (G-22).
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only (G-20)');
END;

CREATE TRIGGER audit_log_guarded_delete BEFORE DELETE ON audit_log
WHEN old.seq > COALESCE(
    (SELECT CAST(substr(value, 1, instr(value, ':') - 1) AS INTEGER)
       FROM system_state WHERE key = 'writes.audit_checkpoint_hash'), 0)
BEGIN
    SELECT RAISE(ABORT, 'audit_log rows are removed only through the checkpoint (G-22)');
END;

-- Grants are never deleted by use, revocation or expiry (G-2).
CREATE TRIGGER grants_no_delete BEFORE DELETE ON grants
WHEN old.compacted_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'grants are never deleted before compaction (G-2)');
END;

CREATE TRIGGER grants_terminal_is_final BEFORE UPDATE OF state ON grants
WHEN old.state IN ('spent', 'revoked', 'expired', 'declined', 'lapsed')
     AND new.state <> old.state
BEGIN
    SELECT RAISE(ABORT, 'a terminal grant state is final (G-4)');
END;

CREATE TRIGGER grants_immutable BEFORE UPDATE ON grants
WHEN old.state <> 'proposed' AND new.compacted_at IS old.compacted_at AND (
        new.kind <> old.kind
     OR new.source_id <> old.source_id
     OR new.content_sha256 <> old.content_sha256
     OR new.scope_json IS NOT old.scope_json
     OR new.granted_at IS NOT old.granted_at
     OR new.expires_at IS NOT old.expires_at)
BEGIN
    SELECT RAISE(ABORT, 'grant fields are immutable once active (G-3)');
END;

CREATE TRIGGER operations_no_delete BEFORE DELETE ON operations
WHEN old.compacted_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'operations are deleted only after compaction (W-25)');
END;

CREATE TRIGGER operations_intent_immutable BEFORE UPDATE OF intent_sha256 ON operations
WHEN new.intent_sha256 <> old.intent_sha256
BEGIN
    SELECT RAISE(ABORT, 'an operation''s intent hash never changes (W-1)');
END;

CREATE TRIGGER operations_terminal_is_final BEFORE UPDATE OF state ON operations
WHEN old.state IN ('confirmed', 'failed_clean', 'confirmed_absent', 'cancelled')
     AND new.state <> old.state
BEGIN
    SELECT RAISE(ABORT, 'a terminal operation state is final (W-6)');
END;
