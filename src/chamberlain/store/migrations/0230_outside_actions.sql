-- 0230 o-outside: the `agent_reported` operation state, for an outside action the assistant says
-- it did (docs/contracts/04-writes.md W-44 to W-47).
-- The CHECK on `state` cannot be altered, so the table is rebuilt exactly as it stands, with the new
-- state added to the CHECK and to the "terminal is final" trigger. Nothing references it.
-- Several sources in one standing grant (05-grants.md G-26) needs no change here: a scope's new
-- `sources` field is left out of every existing scope, so existing grants keep their stored scope,
-- their content hash and their meaning. Grants are found by `json_each` over the stored scope.

CREATE TABLE operations_new (
    handle           TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (state IN ('proposed', 'authorised', 'prepared', 'dispatched',
                                      'confirmed', 'failed_clean', 'uncertain', 'reconciling',
                                      'confirmed_absent', 'cancelled', 'abandoned',
                                      'superseded', 'agent_reported')),
    grant            TEXT REFERENCES grants (handle),
    card             TEXT,
    session          TEXT,
    intent_sha256    TEXT NOT NULL CHECK (length(intent_sha256) = 64),
    intent_json      TEXT,
    reconcile_token  TEXT,
    refs_json        TEXT,
    prepared_json    TEXT,
    receipt_json     TEXT,
    client_verified  INTEGER NOT NULL DEFAULT 0 CHECK (client_verified IN (0, 1)),
    server_verified  INTEGER NOT NULL DEFAULT 0 CHECK (server_verified IN (0, 1)),
    error_code       TEXT,
    worker           TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    dispatched_at    TEXT,
    settled_at       TEXT,
    compacted_at     TEXT,
    lease_owner      TEXT,
    lease_expires_at TEXT,
    summary_json     TEXT,
    attempted_at     TEXT,
    redo_at          TEXT,
    rechecked_at     TEXT,
    recheck_result   TEXT CHECK (recheck_result IN ('found', 'queued', 'missing')),
    recheck_json     TEXT,
    redo_of          TEXT
) STRICT;

INSERT INTO operations_new (
    handle, idempotency_key, kind, source_id, state, grant, card, session, intent_sha256,
    intent_json, reconcile_token, refs_json, prepared_json, receipt_json, client_verified,
    server_verified, error_code, worker, created_at, updated_at, dispatched_at, settled_at,
    compacted_at, lease_owner, lease_expires_at, summary_json, attempted_at, redo_at,
    rechecked_at, recheck_result, recheck_json, redo_of)
SELECT
    handle, idempotency_key, kind, source_id, state, grant, card, session, intent_sha256,
    intent_json, reconcile_token, refs_json, prepared_json, receipt_json, client_verified,
    server_verified, error_code, worker, created_at, updated_at, dispatched_at, settled_at,
    compacted_at, lease_owner, lease_expires_at, summary_json, attempted_at, redo_at,
    rechecked_at, recheck_result, recheck_json, redo_of
FROM operations;

DROP TABLE operations;
ALTER TABLE operations_new RENAME TO operations;

CREATE INDEX operations_state ON operations (state);
CREATE INDEX operations_grant ON operations (grant, state);
CREATE INDEX operations_source_dispatched ON operations (source_id, dispatched_at);
CREATE UNIQUE INDEX operations_reconcile_token
    ON operations (reconcile_token) WHERE reconcile_token IS NOT NULL;
CREATE UNIQUE INDEX operations_redo_of
    ON operations (redo_of) WHERE redo_of IS NOT NULL AND state <> 'cancelled';

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
WHEN old.state IN ('confirmed', 'failed_clean', 'confirmed_absent', 'cancelled', 'abandoned',
                   'superseded', 'agent_reported')
     AND new.state <> old.state
BEGIN
    SELECT RAISE(ABORT, 'a terminal operation state is final (W-6)');
END;
