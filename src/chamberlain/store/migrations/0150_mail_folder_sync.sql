-- 0150 mail folder sync: before the scan reads a source, the harness asks the mail client to sync
-- every folder the scan reads, waits, and records when each folder was last in sync.
-- Writer: the attention track (src/chamberlain/attention/sync.py). Contract: 07-attention.md A-24 to A-27.
-- Folder names are kept so a report can name the folders that would not sync; no mail is stored here.

CREATE TABLE mail_sync (
    source_id     TEXT PRIMARY KEY,
    complete_at   TEXT,
    attempted_at  TEXT NOT NULL
) STRICT;

CREATE TABLE mail_sync_folders (
    source_id     TEXT NOT NULL,
    folder_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL,
    synced_at     TEXT,
    attempted_at  TEXT NOT NULL,
    failure       TEXT NOT NULL DEFAULT '',
    catchup_from  TEXT,
    PRIMARY KEY (source_id, folder_id)
) STRICT;

-- How long before a scheduled pass the tick starts the sync (0 turns it off), per schedule.
ALTER TABLE schedules ADD COLUMN presync_minutes INTEGER NOT NULL DEFAULT 5
    CHECK (presync_minutes BETWEEN 0 AND 120);
