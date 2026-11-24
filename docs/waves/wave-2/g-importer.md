# Wave 2, track (g): importer and cutover

You move one existing installation into the new harness without ever changing the old
one. Read `AGENTS.md`, `docs/BOARD.md`, the plan's "Migrating current users' private
data" section, then the contracts below.

## Binding contracts

All contracts, and especially `09-storage.md` (S-14), `04-writes.md`, `05-grants.md`
and `06-memory.md`.

## You own

`src/chamberlain/importer/`, `tests/importer/`, migrations `0060`-`0069`.

## Build

1. `chamberlain import <old data root>`: one shot, read-only on the source (open the old
   SQLite read-only by URI; never write, never run the old harness, which takes backups
   and prunes at start-up). Idempotent: a second run changes nothing.
2. Sources to read: the old `monitor/monitor.db` (one-off and standing approval tables,
   cases and actions, evidence, thread cache, runs, and the `meta` table, whose
   `thunderbird:<op>:<approval>`, `native-*` and `outlook-*` keys are write journals),
   `monitor/profile.json` (mail and calendar sources, platform bindings including
   `transport: google`), `customisations.yaml`, and the Google and iCloud secrets
   folders.
3. Safety rules: any journal entry not in a final state becomes an `uncertain`
   operation, never a retryable one; standing grants import as needing re-confirmation
   in the viewer; grants with no recorded date are due for review at once (G-6); cards
   over a cap import flagged (M-13); private guidance overrides are listed for the
   owner, not imported; secrets move into `keyring`.
4. Writes only through the tracks' repositories, with actor `system:import`.
5. A cutover checklist in `docs/cutover.md`: parallel read-only running, no period in
   which both systems may write, the old data root left untouched as the fallback.

## Done means

The gate passes; tests import a synthetic old data root built by a fixture (never the
owner's data) covering an in-flight uncertain send, a standing grant, an expired grant,
an over-cap card and a `transport: google` binding, and a second import is a no-op.

## Do not touch

Shared files without a board entry, other tracks' directories, the old repository and
the owner's data (the coordinator runs the real import later, against a copy).
