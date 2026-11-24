-- 0060 per-source new-mail watermark and the batch awaiting triage.
-- Writer: the attention track (src/chamberlain/attention/watermark.py). Other tracks only SELECT (S-3).
-- No sender, subject or body is stored here: only identities, times and a suggested card handle.

CREATE TABLE mail_watermarks (
    source_id        TEXT PRIMARY KEY,
    covered_through  TEXT NOT NULL,
    watching_since   TEXT NOT NULL,
    scan_started_at  TEXT,
    scan_until       TEXT,
    gap_from         TEXT,
    gap_to           TEXT
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
    triaged          INTEGER NOT NULL DEFAULT 0 CHECK (triaged IN (0, 1)),
    UNIQUE (source_id, locator)
) STRICT;

CREATE INDEX mail_seen_pending ON mail_seen (source_id, triaged);
