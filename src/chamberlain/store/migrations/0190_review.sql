-- 0190 harness review: what the agent improvises, and the reviews of it.
-- Writer: the review track (src/chamberlain/review/repo.py). Others only SELECT (S-3).
-- Privacy: only tool names and short normalised command summaries live here, never output, mail
-- text or file contents (docs/contracts/06-memory.md, M-40).

-- One row per pattern: a tool the agent used that is not a Chamberlain tool, with a short summary
-- of the command ('' when only the tool name is recorded).
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

CREATE INDEX improv_patterns_seen ON improv_patterns (last_seen_at);

-- Where each transcript file was read up to, so a scan never counts a line twice. The key is a
-- hash of the file's path: no path, project name or session id is kept.
CREATE TABLE improv_files (
    file_key   TEXT PRIMARY KEY CHECK (length(file_key) = 64),
    byte_pos   INTEGER NOT NULL CHECK (byte_pos >= 0),
    size       INTEGER NOT NULL CHECK (size >= 0),
    qualified  INTEGER NOT NULL CHECK (qualified IN (0, 1)),
    seen_at    TEXT NOT NULL
) STRICT;

-- One review round: raised when the interval has passed and the log has new patterns.
CREATE TABLE review_rounds (
    id          INTEGER PRIMARY KEY,
    state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'lapsed')),
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    new_patterns INTEGER NOT NULL CHECK (new_patterns >= 0)
) STRICT;

CREATE INDEX review_rounds_state ON review_rounds (state, opened_at);

-- What the agent found in a round (`keep` kind `finding`). Only a worrying one reaches the owner.
CREATE TABLE review_findings (
    id          INTEGER PRIMARY KEY,
    round       INTEGER NOT NULL REFERENCES review_rounds (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('idea', 'worrying')),
    text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 400),
    created_at  TEXT NOT NULL,
    reported_at TEXT
) STRICT;

CREATE INDEX review_findings_round ON review_findings (round);
