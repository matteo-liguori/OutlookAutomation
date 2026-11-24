-- 0130 report and dashboard (track d-dashboard).
-- Contract: docs/contracts/06-memory.md (lifetimes) and 09-storage.md (tables).
-- (1) run_reports: what the harness itself wrote into a pass's report, taken when the pass
--     settled (the agent's text stays a stored item and is read from there). Writer: runs.
-- (2) local_events: events the owner adds in the viewer. They exist nowhere else. Writer: runs.
-- (3) event_labels: the category (and short title) the agent gave a calendar event. Writer: memory.

CREATE TABLE run_reports (
    run          TEXT PRIMARY KEY REFERENCES runs (handle) ON DELETE CASCADE,
    composed_at  TEXT NOT NULL,
    report_json  TEXT NOT NULL
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

CREATE INDEX local_events_days ON local_events (end_day, start_day);

CREATE TABLE event_labels (
    identity     TEXT PRIMARY KEY CHECK (length(identity) = 64),
    category     TEXT NOT NULL
                 CHECK (category IN ('work', 'personal', 'social', 'travel', 'health',
                                     'occasion', 'home')),
    title        TEXT NOT NULL DEFAULT '' CHECK (length(title) <= 120),
    last_day     TEXT NOT NULL,
    labelled_at  TEXT NOT NULL
) STRICT;

CREATE INDEX event_labels_day ON event_labels (last_day);
