-- 0201 the harness marks mail read after a pass report when the owner turned that on
-- (read_state.reporting_marks_read). One attempt per message, never repeated.
ALTER TABLE mail_seen ADD COLUMN read_marked_at TEXT;
-- The session that was shown the batch holding this message: what a report covers.
ALTER TABLE mail_seen ADD COLUMN delivered_session TEXT;
