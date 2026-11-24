# Wave 1, track (a): the memory system

You build principle 2: everything the agent stores has an owner and an end
date, everything it reads arrives in bounded pieces, and one sweeper erases
what is finished. This brief is self-contained. Read `AGENTS.md`, then
`docs/BOARD.md`, then the contracts below, before writing code.

## Binding contracts

| File | What binds you |
| --- | --- |
| `docs/contracts/06-memory.md` | All of it (M-1 to M-27). This is your specification. |
| `docs/contracts/02-tools.md` | `find`, `open`, `keep`, `forget`, `card` (T-6, T-8 to T-12). |
| `docs/contracts/03-envelope-errors.md` | Envelope, coverage (E-3), content marking (E-7 to E-9), errors. |
| `docs/contracts/01-domain.md` | Handles and refs (D-3 to D-6), time, layering, hashing. |
| `docs/contracts/09-storage.md` | Your tables, one-writer rule, migrations `0010`-`0019`. |
| `docs/contracts/08-ports.md` | Reading rules P-3 to P-8 (you call the read side of the ports). |
| `docs/contracts/10-targets.md` | Principle 2 targets and the `doctor --metrics` keys you supply. |
| `docs/contracts/11-ported-test-intent.md` | The "Memory" and "Read state and coverage" tables. |

## You own

`src/chamberlain/memory/`, `src/chamberlain/domain/cards.py`,
`src/chamberlain/domain/artifacts.py`, `tests/memory/`, migrations
`0010`-`0019`. Tables you alone write: `cards`, `actions`, `card_links`,
`notes`, `evidence`, `evidence_refs`, `artifacts`, `artifact_owners`,
`refs`, `leases`, `cursors`, `quarantine`, and `memory.*` system keys.

## Do not touch

Everything listed as shared in `docs/OWNERSHIP.md`, the other tracks'
directories, the old repository and its data. Do not import
`chamberlain.attention`, `chamberlain.writes` or `chamberlain.connectors`
(the layout test fails if you do). Reach connectors only through
`ctx.sources`, and other tracks only through `ctx.services` and plain
`SELECT`s on their tables.

## Build

1. `memory/repo.py`: all SQL for your tables (S-4).
2. Cards and actions with caps, replace-not-append, versions, closing and
   compaction (M-12 to M-16, lifetime table).
3. Evidence: immutable, referrer-counted, deleted when unreferenced (M-15).
4. The artifact store: content-addressed files, atomic writes, hash checked
   on every read, owners, leases, `import_path` checks (M-4 to M-11).
5. Refs: the `refs` table and deterministic handles for remote items (D-5,
   D-6), including distinct physical copies (M-20).
6. Retrieval: paged lists with a character budget, immutable session-bound
   cursors, `find` within a handle, bounded `open` ranges (M-17 to M-20).
   Remote text goes only into `Content` (E-7). Coverage is honest (E-3):
   carry a port's `complete: false` and gaps into the reply.
7. `memory/sweeper.py`: the one `sweep(ctx, now) -> SweepReport` applying
   the whole lifetime table, idempotent and interruptible (M-21 to M-24),
   including orphan quarantine (M-25, M-26) and budgets (M-27). Owner
   liveness is read from the tables as defined in M-4; for owners in other
   tracks' tables (`operations`, `grants`, `sessions`) you only `SELECT`.
8. `memory/services.py`: `build(ctx_parts) -> (ArtifactStore, EvidenceStore,
   RefResolver)` implementing the protocols in `app/services.py`.
9. `memory/handlers.py`: fill `HANDLERS` for `find`, `open`, `keep`,
   `forget`, `card` and `DOCTOR_CHECKS` with storage checks. `forget` on a
   `grant_` or `op_` handle calls `ctx.services.writes` (T-11).

The sweeper is called by the attention tick (A-6). Expose it as
`chamberlain.memory.sweeper.sweep`; attention will be wired to it at
integration. Do not call into attention yourself: when the sweeper finds
orphans or a budget is passed, record that in your own tables or system
keys, and post an `INTERFACE` entry saying which rows the
`storage.orphans` and `storage.over_budget` detectors should read.

## Read in the old repository (read-only)

`monitor/threadcache.py` (ownership of cached mail, expiry, interrupted
closure), `monitor/evidence.py`, `monitor/cards.py`,
`monitor/card_input.py`, `monitor/maintenance.py`, `monitor/backups.py`,
`monitor/live.py`, and `put_evidence` in `monitor/storage.py`. Tests:
`tests/test_threadcache.py`, `test_evidence_lifecycle.py`, `test_cards.py`,
`test_card_retirement.py`, `test_maintenance.py`, `test_action_cache.py`.
Port the intent in the tables of `11-ported-test-intent.md`. Do not port
code, record kinds (context, decision, issue, roundup, watch) or the six
separate lifecycles.

## Done means

All in `tests/memory/`, each test naming its rule ID:

- every row of the "Memory" table in `11-ported-test-intent.md` has a test;
- the four sweeper properties at the end of `06-memory.md`, as Hypothesis
  stateful tests over random create, own, keep, finish, close, expire,
  sweep and crash-mid-sweep sequences;
- cap tests for every row of the bounded-records table (M-12);
- paging tests: budgets, cursor immutability, expiry, session binding
  (M-17), and no body in any `find` reply (T-8);
- read-state test: `find` and `open` over the fake leave unread mail unread
  (T-9), and a stale fake yields `coverage.complete == false` (E-3);
- artifact tests: no owner refused, hash mismatch never returns bytes,
  interrupted write leaves nothing, symlink and oversize import refused,
  a pending operation's artifact survives expiry (M-11);
- orphan test: a stray file in the data root is quarantined, reported
  once, deleted after the grace period, and nothing outside the data root
  is touched (M-26);
- handler tests through `App.call` for the five tools, with golden replies;
- no reply over `MAX_REPLY_CHARS`, no page over `PAGE_CHARS`;
- the gate is green.

## Report

Post a `HANDOFF` on the board: what exists, the gate result, the service
factory signature, the rows detectors should read, anything in the contract
you had to interpret, and what is not done.
