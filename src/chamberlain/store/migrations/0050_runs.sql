-- Wave 2 track (f): the schedule record, runs, and notes the person leaves for the assistant.
-- Writer: the runs track (src/chamberlain/runs/repo.py). Other tracks only SELECT (S-3).

CREATE TABLE schedules (
    name        TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 40),
    kind        TEXT NOT NULL CHECK (kind IN ('mail', 'calendar')),
    at_local    TEXT NOT NULL CHECK (at_local GLOB '[0-2][0-9]:[0-5][0-9]'),
    weekdays    TEXT NOT NULL CHECK (length(weekdays) BETWEEN 1 AND 13),
    zone        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

-- One row per scheduled pass and local day. A run owns its report artifacts (M-4).
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

CREATE INDEX runs_state ON runs (state, due_at);

-- A short note from the person in the viewer; it reaches the agent as a `user.note` event.
CREATE TABLE user_notes (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 150),
    state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'withdrawn')),
    created_at  TEXT NOT NULL
) STRICT;

CREATE INDEX user_notes_state ON user_notes (state, created_at);
