-- 0001 initial schema.
-- Contract: docs/contracts/09-storage.md. Each table has one writing track
-- (S-3) and one data class (S-6, store/schema.py). Timestamps are UTC ISO 8601
-- text ending in Z (D-7). A *_json column is the dump of one Pydantic model (S-2).

-- ---------------------------------------------------------------- system --

CREATE TABLE system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

-- ------------------------------------------------------------- app: sources

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

-- ---------------------------------------------------------------- attention

CREATE TABLE source_health (
    source_id    TEXT PRIMARY KEY REFERENCES sources (id) ON DELETE CASCADE,
    state        TEXT NOT NULL
                 CHECK (state IN ('ok', 'stale', 'sign_in_needed', 'unavailable')),
    checked_at   TEXT NOT NULL,
    fresh_as_of  TEXT
) STRICT;

CREATE TABLE sessions (
    handle        TEXT PRIMARY KEY,
    agent         TEXT NOT NULL DEFAULT '',
    purpose       TEXT NOT NULL CHECK (purpose IN ('interactive', 'scheduled')),
    started_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    ended_at      TEXT
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

-- At most one live event per condition (A-1).
CREATE UNIQUE INDEX events_live_key ON events (dedupe_key)
    WHERE state IN ('new', 'delivered');
CREATE INDEX events_state_priority ON events (state, priority DESC, detected_at);

CREATE TABLE guidance_deliveries (
    session       TEXT NOT NULL REFERENCES sessions (handle) ON DELETE CASCADE,
    rule_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    scope_key     TEXT NOT NULL,
    delivered_at  TEXT NOT NULL,
    PRIMARY KEY (session, rule_id, scope_key)
) STRICT;

-- ------------------------------------------------------------------- memory

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

CREATE INDEX cards_state ON cards (state);

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
) STRICT;

CREATE INDEX actions_card ON actions (card, state);
CREATE INDEX actions_due ON actions (state, due_at);

CREATE TABLE card_links (
    card                 TEXT NOT NULL REFERENCES cards (handle) ON DELETE CASCADE,
    ref                  TEXT NOT NULL,
    internet_message_id  TEXT,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (card, ref)
) STRICT;

CREATE INDEX card_links_message ON card_links (internet_message_id);

CREATE TABLE notes (
    handle      TEXT PRIMARY KEY,
    action      TEXT NOT NULL REFERENCES actions (handle) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

CREATE INDEX notes_action ON notes (action);

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

CREATE INDEX evidence_refs_owner ON evidence_refs (owner);

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

CREATE INDEX artifacts_sha ON artifacts (sha256);
CREATE INDEX artifacts_state ON artifacts (state, expires_at);

CREATE TABLE artifact_owners (
    artifact    TEXT NOT NULL REFERENCES artifacts (handle) ON DELETE CASCADE,
    owner_kind  TEXT NOT NULL
                CHECK (owner_kind IN ('action', 'operation', 'grant', 'session', 'run')),
    owner       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (artifact, owner)
) STRICT;

CREATE INDEX artifact_owners_owner ON artifact_owners (owner);

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

CREATE TABLE leases (
    id          TEXT PRIMARY KEY,
    artifact    TEXT NOT NULL REFERENCES artifacts (handle) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
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

CREATE TABLE quarantine (
    id                INTEGER PRIMARY KEY,
    original_path     TEXT NOT NULL,
    quarantined_path  TEXT NOT NULL,
    size              INTEGER NOT NULL CHECK (size >= 0),
    found_at          TEXT NOT NULL,
    delete_after      TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'deleted'))
) STRICT;

-- ------------------------------------------------------------------- writes

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

CREATE INDEX grants_state ON grants (state, source_id);

CREATE TABLE known_recipients (
    source_id     TEXT NOT NULL,
    address       TEXT NOT NULL,
    grant         TEXT NOT NULL REFERENCES grants (handle),
    confirmed_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, address)
) STRICT;

CREATE TABLE operations (
    handle           TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (state IN ('proposed', 'authorised', 'prepared', 'dispatched',
                                      'confirmed', 'failed_clean', 'uncertain', 'reconciling',
                                      'confirmed_absent', 'cancelled')),
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
    compacted_at     TEXT
) STRICT;

CREATE INDEX operations_state ON operations (state);
CREATE INDEX operations_grant ON operations (grant, state);
CREATE INDEX operations_source_dispatched ON operations (source_id, dispatched_at);

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

CREATE INDEX audit_log_subject ON audit_log (subject);
