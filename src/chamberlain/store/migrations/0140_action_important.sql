-- 0140 an action may be marked important by the agent (owner ruling 2026-11-24): an important
-- appointment is its own flag, not a title prefix. Actions titled "Important..." before this
-- migration keep showing as important.
ALTER TABLE actions ADD COLUMN important INTEGER NOT NULL DEFAULT 0 CHECK (important IN (0, 1));
UPDATE actions SET important = 1 WHERE lower(title) LIKE 'important%';
