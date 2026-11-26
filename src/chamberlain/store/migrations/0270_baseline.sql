-- Single current schema, equivalent to the Step 3 migration chain.

CREATE TABLE actions (
    handle        TEXT PRIMARY KEY,
    card          TEXT NOT NULL REFERENCES cards (handle) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'dropped')),
    due_at        TEXT,
    snooze_until  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT,
    outcome       TEXT,
    purge_at      TEXT
, important INTEGER NOT NULL DEFAULT 0 CHECK (important IN (0, 1))) STRICT;

CREATE TABLE "artifact_owners" (
    artifact    TEXT NOT NULL REFERENCES artifacts (handle) ON DELETE CASCADE,
    owner_kind  TEXT NOT NULL
                CHECK (owner_kind IN ('action', 'operation', 'grant', 'session', 'run', 'card')),
    owner       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (artifact, owner)
) STRICT;

CREATE TABLE artifacts (
    handle      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL
                CHECK (kind IN ('mail_body', 'attachment', 'download', 'import', 'text')),
    media_type  TEXT NOT NULL,
    size        INTEGER NOT NULL CHECK (size >= 0),
    sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
    filename    TEXT,
    reason      TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'released', 'deleted')),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    deleted_at  TEXT
) STRICT;

CREATE TABLE audit_log (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    actor           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    subject         TEXT,
    summary         TEXT NOT NULL DEFAULT '',
    payload_sha256  TEXT,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL
) STRICT;

CREATE TABLE card_links (
    card                 TEXT NOT NULL REFERENCES cards (handle) ON DELETE CASCADE,
    ref                  TEXT NOT NULL,
    internet_message_id  TEXT,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (card, ref)
) STRICT;

CREATE TABLE cards (
    handle      TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    goal        TEXT NOT NULL DEFAULT '',
    position    TEXT NOT NULL DEFAULT '',
    rules       TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    version     INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    over_cap    INTEGER NOT NULL DEFAULT 0 CHECK (over_cap IN (0, 1)),
    review_at   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    closed_at   TEXT,
    outcome     TEXT,
    purge_at    TEXT
) STRICT;

CREATE TABLE complex_cards (
    card           TEXT PRIMARY KEY REFERENCES cards (handle) ON DELETE CASCADE,
    state          TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'declined')),
    reason         TEXT NOT NULL,
    requested_at   TEXT NOT NULL,
    decided_at     TEXT,
    blocked_until  TEXT,
    CHECK ((state = 'pending') = (decided_at IS NULL))
) STRICT;

CREATE TABLE cursors (
    id            TEXT PRIMARY KEY,
    session       TEXT NOT NULL REFERENCES sessions (handle) ON DELETE CASCADE,
    tool          TEXT NOT NULL,
    query_sha256  TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
) STRICT;

CREATE TABLE event_labels (
    identity     TEXT PRIMARY KEY CHECK (length(identity) = 64),
    category     TEXT NOT NULL
                 CHECK (category IN ('work', 'personal', 'social', 'travel', 'health',
                                     'occasion', 'home')),
    title        TEXT NOT NULL DEFAULT '' CHECK (length(title) <= 120),
    last_day     TEXT NOT NULL,
    labelled_at  TEXT NOT NULL
) STRICT;

CREATE TABLE events (
    handle        TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    subject       TEXT,
    dedupe_key    TEXT NOT NULL,
    priority      INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 100),
    summary       TEXT NOT NULL,
    attrs_json    TEXT NOT NULL DEFAULT '{}',
    state         TEXT NOT NULL DEFAULT 'new'
                  CHECK (state IN ('new', 'delivered', 'handled', 'expired')),
    detected_at   TEXT NOT NULL,
    delivered_at  TEXT,
    delivered_to  TEXT,
    closed_at     TEXT,
    quiet_until   TEXT
) STRICT;

CREATE TABLE evidence (
    handle          TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    ref             TEXT,
    subject_sha256  TEXT,
    session         TEXT,
    created_at      TEXT NOT NULL
) STRICT;

CREATE TABLE evidence_refs (
    evidence    TEXT NOT NULL REFERENCES evidence (handle) ON DELETE CASCADE,
    owner_kind  TEXT NOT NULL CHECK (owner_kind IN ('card', 'action', 'grant', 'operation')),
    owner       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (evidence, owner)
) STRICT;

CREATE TABLE grants (
    handle            TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('one_off', 'standing')),
    state             TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (state IN ('proposed', 'active', 'suspended', 'spent',
                                       'revoked', 'expired', 'declined', 'lapsed')),
    source_id         TEXT NOT NULL,
    title             TEXT NOT NULL,
    summary           TEXT NOT NULL DEFAULT '',
    requirement       TEXT NOT NULL
                      CHECK (requirement IN ('none', 'agent_evidence', 'human_confirmation')),
    content_sha256    TEXT NOT NULL CHECK (length(content_sha256) = 64),
    scope_json        TEXT,
    card              TEXT,
    supersedes        TEXT REFERENCES grants (handle),
    proposed_by       TEXT,
    proposed_at       TEXT NOT NULL,
    confirmed_via     TEXT CHECK (confirmed_via IN ('agent_evidence', 'human_viewer', 'import')),
    granted_at        TEXT,
    expires_at        TEXT,
    review_at         TEXT,
    last_reviewed_at  TEXT,
    ended_at          TEXT,
    compacted_at      TEXT
) STRICT;

CREATE TABLE guidance_deliveries (
    session       TEXT NOT NULL REFERENCES sessions (handle) ON DELETE CASCADE,
    rule_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    scope_key     TEXT NOT NULL,
    delivered_at  TEXT NOT NULL,
    PRIMARY KEY (session, rule_id, scope_key)
) STRICT;

CREATE TABLE imported_context (
    case_id      TEXT PRIMARY KEY,
    card         TEXT NOT NULL,
    artifact     TEXT NOT NULL,
    imported_at  TEXT NOT NULL
) STRICT;

CREATE TABLE imported_items (
    kind         TEXT NOT NULL,
    old_id       TEXT NOT NULL,
    target       TEXT NOT NULL,
    imported_at  TEXT NOT NULL,
    PRIMARY KEY (kind, old_id)
) STRICT;

CREATE TABLE improv_files (
    file_key   TEXT PRIMARY KEY CHECK (length(file_key) = 64),
    byte_pos   INTEGER NOT NULL CHECK (byte_pos >= 0),
    size       INTEGER NOT NULL CHECK (size >= 0),
    qualified  INTEGER NOT NULL CHECK (qualified IN (0, 1)),
    seen_at    TEXT NOT NULL
) STRICT;

CREATE TABLE improv_patterns (
    agent         TEXT NOT NULL CHECK (agent IN ('claude', 'codex')),
    tool          TEXT NOT NULL CHECK (length(tool) BETWEEN 1 AND 80),
    pattern       TEXT NOT NULL CHECK (length(pattern) <= 60),
    total         INTEGER NOT NULL CHECK (total >= 1),
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    added_at      TEXT NOT NULL,
    PRIMARY KEY (agent, tool, pattern)
) STRICT;

CREATE TABLE "known_recipients" (
    source_id     TEXT NOT NULL,
    address       TEXT NOT NULL,
    grant         TEXT REFERENCES grants (handle),
    origin        TEXT NOT NULL DEFAULT 'confirmed-grant'
                  CHECK (origin IN ('confirmed-grant', 'sent-history')),
    confirmed_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, address),
    CHECK ((origin = 'confirmed-grant') = (grant IS NOT NULL))
) STRICT;

CREATE TABLE leases (
    id          TEXT PRIMARY KEY,
    artifact    TEXT NOT NULL REFERENCES artifacts (handle) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
) STRICT;

CREATE TABLE local_events (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 180),
    location    TEXT NOT NULL DEFAULT '' CHECK (length(location) <= 600),
    all_day     INTEGER NOT NULL CHECK (all_day IN (0, 1)),
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    start_day   TEXT NOT NULL,
    end_day     TEXT NOT NULL,
    created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE mail_seen (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        TEXT NOT NULL,
    locator          TEXT NOT NULL,
    handle           TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    suggested_card   TEXT,
    delivered_event  TEXT,
    triaged          INTEGER NOT NULL DEFAULT 0 CHECK (triaged IN (0, 1)), message_key TEXT NOT NULL DEFAULT '', sent INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1)), read_marked_at TEXT, delivered_session TEXT,
    UNIQUE (source_id, locator)
) STRICT;

CREATE TABLE mail_sync (
    source_id     TEXT PRIMARY KEY,
    complete_at   TEXT,
    attempted_at  TEXT NOT NULL
) STRICT;

CREATE TABLE mail_sync_folders (
    source_id     TEXT NOT NULL,
    folder_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL,
    synced_at     TEXT,
    attempted_at  TEXT NOT NULL,
    failure       TEXT NOT NULL DEFAULT '',
    catchup_from  TEXT,
    PRIMARY KEY (source_id, folder_id)
) STRICT;

CREATE TABLE mail_watermarks (
    source_id        TEXT PRIMARY KEY,
    covered_through  TEXT NOT NULL,
    watching_since   TEXT NOT NULL,
    scan_started_at  TEXT,
    scan_until       TEXT,
    gap_from         TEXT,
    gap_to           TEXT
, scan_from TEXT, scan_width INTEGER, held_from TEXT, held_to TEXT, held_state TEXT
    CHECK (held_state IN ('asking', 'approved', 'declined')), held_estimate INTEGER, held_width INTEGER) STRICT;

CREATE TABLE notes (
    handle      TEXT PRIMARY KEY,
    action      TEXT NOT NULL REFERENCES actions (handle) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

CREATE TABLE "operations" (
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

CREATE TABLE owl_freshness (
    source_id TEXT PRIMARY KEY,
    calendar_id TEXT NOT NULL,
    token TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    verified_at TEXT
) STRICT;

CREATE TABLE quarantine (
    id                INTEGER PRIMARY KEY,
    original_path     TEXT NOT NULL,
    quarantined_path  TEXT NOT NULL,
    size              INTEGER NOT NULL CHECK (size >= 0),
    found_at          TEXT NOT NULL,
    delete_after      TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'deleted'))
) STRICT;

CREATE TABLE refs (
    handle               TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL CHECK (kind IN ('message', 'calendar_event', 'contact')),
    source_id            TEXT NOT NULL,
    locator              TEXT NOT NULL,
    internet_message_id  TEXT,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    UNIQUE (source_id, kind, locator)
) STRICT;

CREATE TABLE review_findings (
    id          INTEGER PRIMARY KEY,
    round       INTEGER NOT NULL REFERENCES review_rounds (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('idea', 'worrying')),
    text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 400),
    created_at  TEXT NOT NULL,
    reported_at TEXT
) STRICT;

CREATE TABLE review_rounds (
    id          INTEGER PRIMARY KEY,
    state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'lapsed')),
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    new_patterns INTEGER NOT NULL CHECK (new_patterns >= 0)
) STRICT;

CREATE TABLE run_reports (
    run          TEXT PRIMARY KEY REFERENCES runs (handle) ON DELETE CASCADE,
    composed_at  TEXT NOT NULL,
    report_json  TEXT NOT NULL
) STRICT;

CREATE TABLE runs (
    handle       TEXT PRIMARY KEY,
    schedule     TEXT NOT NULL REFERENCES schedules (name) ON DELETE CASCADE,
    day          TEXT NOT NULL CHECK (day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    state        TEXT NOT NULL DEFAULT 'due'
                 CHECK (state IN ('due', 'running', 'finished', 'incomplete', 'missed')),
    due_at       TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    session      TEXT,
    UNIQUE (schedule, day)
) STRICT;

CREATE TABLE schedules (
    name        TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 40),
    kind        TEXT NOT NULL CHECK (kind IN ('mail', 'calendar')),
    at_local    TEXT NOT NULL CHECK (at_local GLOB '[0-2][0-9]:[0-5][0-9]'),
    weekdays    TEXT NOT NULL CHECK (length(weekdays) BETWEEN 1 AND 13),
    zone        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
, presync_minutes INTEGER NOT NULL DEFAULT 5
    CHECK (presync_minutes BETWEEN 0 AND 120)) STRICT;

CREATE TABLE sessions (
    handle        TEXT PRIMARY KEY,
    agent         TEXT NOT NULL DEFAULT '',
    purpose       TEXT NOT NULL CHECK (purpose IN ('interactive', 'scheduled')),
    started_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    ended_at      TEXT
) STRICT;

CREATE TABLE source_health (
    source_id    TEXT PRIMARY KEY REFERENCES sources (id) ON DELETE CASCADE,
    state        TEXT NOT NULL
                 CHECK (state IN ('ok', 'stale', 'sign_in_needed', 'unavailable')),
    checked_at   TEXT NOT NULL,
    fresh_as_of  TEXT
) STRICT;

CREATE TABLE sources (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('mail', 'calendar', 'contacts')),
    connector     TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    binding_json  TEXT NOT NULL DEFAULT '{}',
    state         TEXT NOT NULL DEFAULT 'enabled' CHECK (state IN ('enabled', 'disabled')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
) STRICT;

CREATE TABLE system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

CREATE TABLE user_notes (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 150),
    state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'withdrawn')),
    created_at  TEXT NOT NULL
) STRICT;

CREATE INDEX actions_card ON actions (card, state);

CREATE INDEX actions_due ON actions (state, due_at);

CREATE INDEX artifact_owners_owner ON artifact_owners (owner);

CREATE INDEX artifacts_sha ON artifacts (sha256);

CREATE INDEX artifacts_state ON artifacts (state, expires_at);

CREATE INDEX audit_log_subject ON audit_log (subject);

CREATE INDEX card_links_message ON card_links (internet_message_id);

CREATE INDEX cards_state ON cards (state);

CREATE INDEX event_labels_day ON event_labels (last_day);

CREATE UNIQUE INDEX events_live_key ON events (dedupe_key)
    WHERE state IN ('new', 'delivered');

CREATE INDEX events_state_priority ON events (state, priority DESC, detected_at);

CREATE INDEX evidence_refs_owner ON evidence_refs (owner);

CREATE INDEX grants_state ON grants (state, source_id);

CREATE INDEX improv_patterns_seen ON improv_patterns (last_seen_at);

CREATE INDEX local_events_days ON local_events (end_day, start_day);

CREATE UNIQUE INDEX mail_seen_key ON mail_seen (source_id, message_key);

CREATE INDEX mail_seen_pending ON mail_seen (source_id, triaged);

CREATE INDEX notes_action ON notes (action);

CREATE INDEX operations_grant ON operations (grant, state);

CREATE UNIQUE INDEX operations_reconcile_token
    ON operations (reconcile_token) WHERE reconcile_token IS NOT NULL;

CREATE UNIQUE INDEX operations_redo_of
    ON operations (redo_of) WHERE redo_of IS NOT NULL AND state <> 'cancelled';

CREATE INDEX operations_source_dispatched ON operations (source_id, dispatched_at);

CREATE INDEX operations_state ON operations (state);

CREATE INDEX review_findings_round ON review_findings (round);

CREATE INDEX review_rounds_state ON review_rounds (state, opened_at);

CREATE INDEX runs_state ON runs (state, due_at);

CREATE INDEX user_notes_state ON user_notes (state, created_at);

CREATE TRIGGER audit_log_guarded_delete BEFORE DELETE ON audit_log
WHEN old.seq > COALESCE(
    (SELECT CAST(substr(value, 1, instr(value, ':') - 1) AS INTEGER)
       FROM system_state WHERE key = 'writes.audit_checkpoint_hash'), 0)
BEGIN
    SELECT RAISE(ABORT, 'audit_log rows are removed only through the checkpoint (G-22)');
END;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only (G-20)');
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

CREATE TRIGGER operations_intent_immutable BEFORE UPDATE OF intent_sha256 ON operations
WHEN new.intent_sha256 <> old.intent_sha256
BEGIN
    SELECT RAISE(ABORT, 'an operation''s intent hash never changes (W-1)');
END;

CREATE TRIGGER operations_no_delete BEFORE DELETE ON operations
WHEN old.compacted_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'operations are deleted only after compaction (W-25)');
END;

CREATE TRIGGER operations_terminal_is_final BEFORE UPDATE OF state ON operations
WHEN old.state IN ('confirmed', 'failed_clean', 'confirmed_absent', 'cancelled', 'abandoned',
                   'superseded', 'agent_reported')
     AND new.state <> old.state
BEGIN
    SELECT RAISE(ABORT, 'a terminal operation state is final (W-6)');
END;
