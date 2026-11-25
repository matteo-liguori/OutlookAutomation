-- 0200 one row per old record the importer has carried into v2 (card rules, important flags,
-- owner instruction topics, proposed read-state grants), so a second import adds only what is
-- missing and never brings back what the owner or the agent has since changed or removed.
CREATE TABLE imported_items (
    kind         TEXT NOT NULL,
    old_id       TEXT NOT NULL,
    target       TEXT NOT NULL,
    imported_at  TEXT NOT NULL,
    PRIMARY KEY (kind, old_id)
) STRICT;
