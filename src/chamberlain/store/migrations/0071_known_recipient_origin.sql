-- 0071 writes track: a recipient can become known from Sent history, not only from a
-- human-confirmed grant. `grant` is now optional and `origin` says how the row arose.
-- Contract: docs/contracts/05-grants.md (G-15), owner decision 2026-11-24.

CREATE TABLE known_recipients_new (
    source_id     TEXT NOT NULL,
    address       TEXT NOT NULL,
    grant         TEXT REFERENCES grants (handle),
    origin        TEXT NOT NULL DEFAULT 'confirmed-grant'
                  CHECK (origin IN ('confirmed-grant', 'sent-history')),
    confirmed_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, address),
    CHECK ((origin = 'confirmed-grant') = (grant IS NOT NULL))
) STRICT;

INSERT INTO known_recipients_new (source_id, address, grant, origin, confirmed_at)
    SELECT source_id, lower(address), grant, 'confirmed-grant', confirmed_at
    FROM known_recipients
    GROUP BY source_id, lower(address);

DROP TABLE known_recipients;
ALTER TABLE known_recipients_new RENAME TO known_recipients;
