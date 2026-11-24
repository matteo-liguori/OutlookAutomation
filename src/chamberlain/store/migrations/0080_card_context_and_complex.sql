-- 0080 memory track: (1) a card may hold a stored text item (the context carried over from the
-- old harness), (2) an import ledger for that context, (3) owner-approved larger cards.
-- Contract: docs/contracts/06-memory.md (M-4, M-12, M-28, M-29).

-- (1) `card` becomes an owner kind of artifacts. SQLite cannot change a CHECK, so rebuild.
CREATE TABLE artifact_owners_new (
    artifact    TEXT NOT NULL REFERENCES artifacts (handle) ON DELETE CASCADE,
    owner_kind  TEXT NOT NULL
                CHECK (owner_kind IN ('action', 'operation', 'grant', 'session', 'run', 'card')),
    owner       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (artifact, owner)
) STRICT;

INSERT INTO artifact_owners_new (artifact, owner_kind, owner, created_at)
    SELECT artifact, owner_kind, owner, created_at FROM artifact_owners;

DROP TABLE artifact_owners;
ALTER TABLE artifact_owners_new RENAME TO artifact_owners;
CREATE INDEX artifact_owners_owner ON artifact_owners (owner);

-- (2) One row per old case whose context has been carried over, so a second import (or an
-- agent that has since forgotten the item) never adds it again.
CREATE TABLE imported_context (
    case_id      TEXT PRIMARY KEY,
    card         TEXT NOT NULL,
    artifact     TEXT NOT NULL,
    imported_at  TEXT NOT NULL
) STRICT;

-- (3) One row per card that asked for, or holds, a larger cap. Only the owner (the viewer)
-- moves a row out of `pending`. A revoked card has no row. `blocked_until` is set on a decline.
CREATE TABLE complex_cards (
    card           TEXT PRIMARY KEY REFERENCES cards (handle) ON DELETE CASCADE,
    state          TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'declined')),
    reason         TEXT NOT NULL,
    requested_at   TEXT NOT NULL,
    decided_at     TEXT,
    blocked_until  TEXT,
    CHECK ((state = 'pending') = (decided_at IS NULL))
) STRICT;
