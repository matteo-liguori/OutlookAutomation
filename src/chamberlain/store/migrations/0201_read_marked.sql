-- 0201 the harness marks mail read after a pass report when the owner turned that on
-- (read_state.reporting_marks_read). One attempt per message, never repeated.
ALTER TABLE mail_seen ADD COLUMN read_marked_at TEXT;
