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
| `owl_freshness` | Working set | app (Owl freshness adapter) | Probe ownership, recovery and last verified freshness per calendar binding |
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
| `imported_items` | Records | memory | One row per old record the importer carried over (card rules, important flags, owner instruction topics, proposed read-state grants), so a repeat import adds only what is missing (M-41) |
| `imported_context` | Records | memory | Which old cases already had their context carried over (M-35) |
| `complex_cards` | Records | memory | Requests for, and owner answers on, a larger card limit (M-36 to M-38) |
| `refs` | Working set | memory | Handle to source and locator |
| `leases` | Ephemeral | memory | Leased file paths |
| `cursors` | Ephemeral | memory | Page cursors |
| `quarantine` | System | memory | Orphans awaiting deletion |
| `grants` | Records | writes | Grants |
| `known_recipients` | Records | writes | Known recipients per source: confirmed by a grant, or seeded from Sent history (`origin`) |
| `operations` | Records | writes | Write operations |
| `audit_log` | System | writes | Hash-chained audit rows |
| `mail_watermarks` | Records | attention | Per source: mail covered through a time, an in-progress oldest-first scan, the uncovered span, and the older span held for the owner's answer (A-23) |
| `mail_seen` | Records | attention | One row per message (message key, whatever folders hold copies): time, suggested card, sent or received; the batch awaiting triage |
| `mail_sync` | Records | attention | Per source: when every folder the scan reads was last in sync, and the last try (A-24) |
| `mail_sync_folders` | Records | attention | Per source and folder: name, role, last sync, last failure and where an on-its-own catch-up read starts (A-25, A-27) |
| `schedules` | Records | runs | The schedule record: which passes are due, when |
| `runs` | Records | runs | One row per scheduled pass and day; owns its report artifacts |
| `user_notes` | Ephemeral | runs | Short notes from the viewer, raised as `user.note` |
| `run_reports` | Records | runs | What the harness wrote into a pass's report when the pass settled (M-39) |
| `local_events` | Records | runs | Events the owner adds in the viewer; no calendar holds them (M-39) |
| `event_labels` | Records | memory | The category and short title the agent gave a calendar event (T-10b, M-39) |
| `improv_patterns` | Records | review | Tools the agent used outside Chamberlain, a short command summary, count and times (M-40) |
| `improv_files` | Ephemeral | review | How far each agent transcript file has been read (a hash of its path, a byte position) |
| `review_rounds` | Records | review | One row per harness review: open, done or lapsed |
| `review_findings` | Records | review | What the agent found in a round: an idea, or something worrying (T-10c) |

Each table has one owning module. Other modules use its queries and services;
they do not write its tables directly. The app wires the services into `Context`.

Column definitions are in `0270_baseline.sql`; that file is normative.

## Migrations

- **S-8** A fresh store starts with `0270_baseline.sql`. A later migration is
  `NNNN_snake_name.sql` in `store/migrations/`, applied in ascending number
  order in one transaction with its `schema_migrations` row (version, name,
  SHA-256 of the file, time). The narrow 0250-to-0260 data bridge is not a
  migration for a fresh store.
- **S-9** Applied migrations are immutable. On start the runner compares
  checksums with the migration file or the retained pre-baseline checksum
  manifest and refuses a mismatch (`store.migration_failed`). Fix forward
  with a new migration.
- **S-10** Before applying any pending migration to a database that already
  has one applied, the runner writes a backup with SQLite's online backup
  API to `backups/pre-migration-<NNNN>.sqlite3`. A failed migration rolls
  back and leaves the database at the previous version.
- **S-11** The runner refuses a database whose highest applied version is
  newer than the code knows, and refuses a pending migration numbered below
  the highest applied one (out of order). In development, delete the
  database; before any release, renumber.
- **S-12** New migrations are numbered above the highest one in the tree;
  gaps are allowed. Connectors do not touch SQLite (D-10).
- **S-13** A migration that changes a shared table is a contract change;
  update its contract and proving tests with it.
- **S-14** Migrations are SQL only. Data transformations that need Python
  belong in the importer, which uses the owning modules' repositories.
- **S-15** While the project is alpha, there is no downgrade path and no
  compatibility promise between versions other than the permanent importer
  from the old harness. A known 0250 or 0260 store is adopted in place after
  ledger and schema verification; its historical ledger is retained. A 0250
  store receives the 0260 calendar-plan conversion before the baseline stamp.
  Unrecognized or partial stores are refused without changing their records.

## Configuration and secrets

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

Migration 0300 adds `grants.action`: the owner's card action a held send waits on (W-48);
existing grants name none.

Migration 0290 adds `actions.for_owner` (0 for existing actions) and `run_reports.frozen_at`
(set when the next scheduled pass starts and freezes the report).

Migration 0280 converts old single-message move and trash intents to one-item batches. It preserves the original approval text and hash, checks the original Message-ID before dispatch and keeps pending operations discoverable under either representation. A legacy Thunderbird recovery plan that lacks the new batch evidence blocks upgrade. Doctor names the affected operation so its matching bridge can reconcile it before upgrading.
