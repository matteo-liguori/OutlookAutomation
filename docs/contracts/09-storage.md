# 09 Storage and migrations

Binding for every track that touches SQLite. Code:
`src/chamberlain/store/`, schema in `src/chamberlain/store/migrations/`.

## Rules

- **S-1** One SQLite file, `<data root>/chamberlain.sqlite3`, opened only
  through `store.db.connect`, which sets `foreign_keys = ON`,
  `journal_mode = WAL`, `busy_timeout = 5000` and returns rows by name.
- **S-2** Tables are `STRICT`, with real columns and `CHECK` constraints on
  every state column. A column ending `_json` holds the JSON dump of one
  named Pydantic model and nothing else; logic MUST NOT query inside it with
  SQL JSON functions. If you need to filter on it, it should be a column.
- **S-3** **Each table has exactly one writing track** (below). Other
  tracks may read it with plain `SELECT` and MUST NOT insert, update or
  delete. The shared schema is the integration surface between tracks; a
  track's Python internals are not.
- **S-4** SQL lives in repository modules inside the owning track
  (`memory/repo.py`, `writes/repo.py`, `attention/repo.py`). No SQL strings
  in handlers, detectors, the app or surfaces. No ORM.
- **S-5** All writes for one logical change are one transaction
  (`store.db.transaction`). Nested use joins the outer transaction.
- **S-6** Every table has a data class (06) in `store.schema.TABLE_CLASSES`
  and a lifetime line in the memory contract. A test compares the live
  schema with that mapping in both directions.
- **S-7** Timestamps are `TEXT` in the D-7 form. Booleans are `INTEGER`
  0 or 1. Handles are `TEXT` primary keys.

## Tables

| Table | Class | Writer | Purpose |
| --- | --- | --- | --- |
| `schema_migrations` | System | store | Applied migrations and checksums |
| `system_state` | System | any, own keys only | Closed set of keys (`store.schema.SYSTEM_KEYS`) |
| `sources` | Records | app | Enabled sources and their bindings |
| `source_health` | Ephemeral | attention | Last health per source |
| `sessions` | Ephemeral | attention | Agent sessions |
| `events` | Ephemeral | attention | The event inbox |
| `guidance_deliveries` | Ephemeral | attention | Per-session delivery ledger |
| `cards` | Records | memory | Cards |
| `actions` | Records | memory | Actions |
| `card_links` | Records | memory | Which messages and events belong to a card |
| `notes` | Working set | memory | Action-scoped notes |
| `evidence` | Evidence | memory | Immutable statements |
| `evidence_refs` | Evidence | memory | Who refers to each evidence row |
| `artifacts` | Working set | memory | Artifact rows |
| `artifact_owners` | Working set | memory | Owner holds |
| `refs` | Working set | memory | Handle to source and locator |
| `leases` | Ephemeral | memory | Leased file paths |
| `cursors` | Ephemeral | memory | Page cursors |
| `quarantine` | System | memory | Orphans awaiting deletion |
| `grants` | Records | writes | Grants |
| `known_recipients` | Records | writes | Known recipients per source: confirmed by a grant, or seeded from Sent history (`origin`) |
| `operations` | Records | writes | Write operations |
| `audit_log` | System | writes | Hash-chained audit rows |
| `mail_watermarks` | Records | attention | Per source: mail covered through a time, an in-progress scan, the uncovered span |
| `mail_seen` | Records | attention | Identity, time and suggested card of each new message; the batch awaiting triage |
| `schedules` | Records | runs | The schedule record: which passes are due, when |
| `runs` | Records | runs | One row per scheduled pass and day; owns its report artifacts |
| `user_notes` | Ephemeral | runs | Short notes from the viewer, raised as `user.note` |

Cross-track needs that are not a plain read go through the service
protocols in `src/chamberlain/app/services.py` (`AuditLog` and
`WriteControl` from writes; `ArtifactStore`, `EvidenceStore` and
`RefResolver` from memory), which the owning track implements and the app
wires into `Context.services`. For example writes reads attachment bytes
through `ArtifactStore` and looks up locators through `RefResolver`, and
every track appends audit rows through `AuditLog`. Until the real ones are
wired, tests use the in-memory doubles in `src/chamberlain/app/doubles.py`.

Column definitions are in `0001_initial.sql`; that file is normative.

## Migrations

- **S-8** A migration is `NNNN_snake_name.sql` in `store/migrations/`,
  applied in ascending number order, each in one transaction together with
  its `schema_migrations` row (version, name, SHA-256 of the file, time).
- **S-9** Applied migrations are immutable. On start the runner compares
  checksums and refuses to run if an applied file has changed
  (`store.migration_failed`). Fix forward with a new migration.
- **S-10** Before applying any pending migration to a database that already
  has one applied, the runner writes a backup with SQLite's online backup
  API to `backups/pre-migration-<NNNN>.sqlite3`. A failed migration rolls
  back and leaves the database at the previous version.
- **S-11** The runner refuses a database whose highest applied version is
  newer than the code knows, and refuses a pending migration numbered below
  the highest applied one (out of order). In development, delete the
  database; before any release, renumber.
- **S-12** Number ranges avoid collisions between parallel tracks:
  `0001-0009` skeleton, `0010-0019` memory, `0020-0029` attention,
  `0030-0039` writes, `0040-0049` wave 2, `0050-0059` viewer and runs, `0060-0069` mail awareness. Gaps are allowed. Connectors have
  no range because they do not touch SQLite (D-10).
- **S-13** A migration that changes a table another track reads is a
  contract change and needs a board entry before it lands (00-index).
- **S-14** Migrations are SQL only. Data transformations that need Python
  belong in the importer (wave 2), which writes through the tracks'
  repositories.
- **S-15** While the project is alpha, there is no downgrade path and no
  compatibility promise between versions other than the one-shot importer
  from the old harness.

## Configuration and secrets (wave 2 detail, fixed here)

- **S-16** Three layers, each a versioned Pydantic model: shared defaults
  (in the package), private settings and the private connection profile
  (both under `<data root>/config/`). No module reads them at import
  (D-12).
- **S-17** Secrets go to the operating system credential store. No token or
  password is written to the database, the config files, a log or an
  artifact.
- **S-18** The data root is given by `CHAMBERLAIN_DATA` or the `--data`
  option; there is no default inside the repository, and tests always use a
  temporary directory.
