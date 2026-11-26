-- Durable ownership of an enrolled Owl freshness probe, including crash recovery.
CREATE TABLE owl_freshness (
    source_id TEXT PRIMARY KEY,
    calendar_id TEXT NOT NULL,
    token TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    verified_at TEXT
) STRICT;
