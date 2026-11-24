-- 0120 mail scan over every folder: one identity per message, sent mail, a held older span.
-- Writer: the attention track (src/chamberlain/attention/watermark.py). Contract: 07-attention.md A-20 to A-23.
-- Still no sender, subject or body here: only identities, times and a suggested card handle.

-- One row per message, whatever folders hold copies of it (owner ruling 2026-11-24).
ALTER TABLE mail_seen ADD COLUMN message_key TEXT NOT NULL DEFAULT '';
ALTER TABLE mail_seen ADD COLUMN sent INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1));

UPDATE mail_seen SET message_key = COALESCE(
    NULLIF(
        lower(trim(trim((SELECT r.internet_message_id FROM refs r WHERE r.handle = mail_seen.handle)), '<>')),
        ''
    ),
    'loc:' || locator
);

-- Keep the oldest row of any message the old per-locator rule stored twice.
DELETE FROM mail_seen
    WHERE seq NOT IN (SELECT MIN(seq) FROM mail_seen GROUP BY source_id, message_key);

CREATE UNIQUE INDEX mail_seen_key ON mail_seen (source_id, message_key);

-- The scan now walks oldest first and keeps its frontier across ticks.
-- `scan_until` (newest-first frontier) is no longer used; an unfinished old scan restarts safely.
UPDATE mail_watermarks SET scan_until = NULL;
ALTER TABLE mail_watermarks ADD COLUMN scan_from TEXT;
ALTER TABLE mail_watermarks ADD COLUMN scan_width INTEGER;

-- An older span the harness did not read because it is more than a day back (A-23).
-- `held_from` moves forward as the owner-approved catch-up reads it.
ALTER TABLE mail_watermarks ADD COLUMN held_from TEXT;
ALTER TABLE mail_watermarks ADD COLUMN held_to TEXT;
ALTER TABLE mail_watermarks ADD COLUMN held_state TEXT
    CHECK (held_state IN ('asking', 'approved', 'declined'));
ALTER TABLE mail_watermarks ADD COLUMN held_estimate INTEGER;
ALTER TABLE mail_watermarks ADD COLUMN held_width INTEGER;
