-- An action can wait for the owner: their answer, decision or something only they can do.
-- Existing actions do not.
ALTER TABLE actions ADD COLUMN for_owner INTEGER NOT NULL DEFAULT 0 CHECK (for_owner IN (0, 1));

-- When the next scheduled pass started and froze this pass's report. NULL while it may still be
-- live. Older reports are never live (only the newest pass's is), so they need no value here.
ALTER TABLE run_reports ADD COLUMN frozen_at TEXT;
