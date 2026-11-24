# Ownership

Who may change what during wave 1. If a path is not listed under your track,
you do not change it. `tests/test_layout.py` enforces the import boundaries;
this file is the rest.

## Tracks

| Track | Brief | Owns (create and change freely) |
| --- | --- | --- |
| (a) memory | `docs/waves/wave-1/a-memory.md` | `src/chamberlain/memory/`, `src/chamberlain/domain/cards.py`, `src/chamberlain/domain/artifacts.py`, `tests/memory/`, migrations `0010`-`0019` |
| (b) attention | `docs/waves/wave-1/b-attention.md` | `src/chamberlain/attention/`, `src/chamberlain/guidance/`, `tests/attention/`, `tests/guidance/`, migrations `0020`-`0029` |
| (c) writes | `docs/waves/wave-1/c-writes.md` | `src/chamberlain/writes/`, `src/chamberlain/domain/grant_rules.py`, `tests/writes/`, migrations `0030`-`0039` |
| (d) thunderbird | `docs/waves/wave-1/d-thunderbird.md` | `src/chamberlain/connectors/thunderbird/` (including `addon/`), `docs/contracts/12-bridge-v2.md`, `tests/thunderbird/`, add-on build files under `src/chamberlain/connectors/thunderbird/addon/` |

A track may add new modules inside its own directories without telling
anyone. A new module that another track will import or a column another
track will read needs an `INTERFACE` entry on the board.

## Shared (change only with a `CONTRACT-CHANGE` board entry)

| Path | Why it is shared |
| --- | --- |
| `docs/contracts/00` to `11` | The specification. |
| `src/chamberlain/schema/` | Envelope, errors, tool shapes: every track and both surfaces. |
| `src/chamberlain/domain/` except the track-owned modules above | Types the ports and the schema are built from. |
| `src/chamberlain/limits.py` | Every number the contracts quote. |
| `src/chamberlain/ports/` | The connector interfaces. |
| `src/chamberlain/store/` including `migrations/0001_initial.sql` and `schema.py` | The schema is the integration surface between tracks. |
| `src/chamberlain/app/` | Entry point and the service protocols between tracks. |
| `src/chamberlain/connectors/fake/` | The reference provider all tracks test against. |
| `src/chamberlain/testing/` | Contract suites and rigs. |
| `src/chamberlain/surface/` | CLI and MCP faces (wave 2 owns `viewer/`). |
| `src/chamberlain/dev.py`, `pyproject.toml`, `.github/`, `tests/conftest.py`, `tests/test_layout.py` | The gate and its configuration. |
| `README.md`, `AGENTS.md`, `docs/OWNERSHIP.md`, `docs/waves/` | Coordination. |

`docs/BOARD.md` is shared and append-only for everyone.

Two shared-file changes are pre-approved, because every track needs them.
Post the board entry and make the change in its own commit:

- adding your new table to `store/schema.py` (`TABLES`) and a lifetime line
  to `docs/contracts/06-memory.md`, together with your migration;
- adding a key with your track's prefix to `store/schema.py`
  (`SYSTEM_KEYS`).

Everything else in a shared file, including a new dependency in
`pyproject.toml` and any new error code, event kind or limit, is an
additive contract change: post first, then commit it alone. If you find a
bug in a shared file, post a `BLOCKER` with the failing test; fix it
yourself only if it is a one-line change that alters no interface.

## Tables: one writer each

`docs/contracts/09-storage.md` lists the writing track of every table.
Other tracks read with `SELECT` and never write. Needs beyond reading go
through `src/chamberlain/app/services.py`:

| Protocol | Implemented by | Used by |
| --- | --- | --- |
| `AuditLog` | writes (`writes/services.py`) | everyone |
| `WriteControl` | writes (`writes/services.py`) | memory (`forget`) |
| `ArtifactStore` | memory (`memory/services.py`) | writes (attachments) |
| `EvidenceStore` | memory (`memory/services.py`) | writes (approvals) |
| `RefResolver` | memory (`memory/services.py`) | writes (locators), attention |

Until integration, test against the doubles in
`src/chamberlain/app/doubles.py`. Do not import another track's package.

## Not owned by any wave-1 track

`src/chamberlain/connectors/google_calendar/`,
`src/chamberlain/connectors/caldav/`, `src/chamberlain/surface/viewer/`,
`src/chamberlain/importer/`: wave 2. `app/wiring.py`: the architect, at
integration.

## Out of bounds for everyone

- `C:\dev\OutlookAutomation` and `C:\dev\OutlookAutomation-data`: read
  only, for behaviour and test intent. Never modify, never run its commands
  against live data, never copy private data into this tree or its tests.
- Any real mailbox, calendar or account.
- Anything outside this worktree, apart from the project `.venv`.
