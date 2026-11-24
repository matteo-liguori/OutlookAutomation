# Wave 1, track (c): writes, grants and the audit log

You build the safety core: no write without verifiable authority, and no
blind resend. This brief is self-contained. Read `AGENTS.md`, then
`docs/BOARD.md`, then the contracts below, before writing code.

## Binding contracts

| File | What binds you |
| --- | --- |
| `docs/contracts/04-writes.md` | All of it (W-1 to W-25). The state machine. |
| `docs/contracts/05-grants.md` | All of it (G-1 to G-23). Grants, confirmation channel, send limits, audit log. |
| `docs/contracts/08-ports.md` | Writing rules P-9 to P-17: what you may assume of a connector and what you may not. |
| `docs/contracts/02-tools.md` | `propose`, `do`, `reconcile` (T-13), and `forget` on grants and operations (T-11). |
| `docs/contracts/03-envelope-errors.md` | `not_authorised`, `conflict`, `uncertain` errors; E-11, E-12. |
| `docs/contracts/01-domain.md` | Canonical form and hashing (D-14, D-15), handles, time, layering. |
| `docs/contracts/09-storage.md` | Your tables, one-writer rule, migrations `0030`-`0039`. |
| `docs/contracts/06-memory.md` | M-4, M-11 (operations and grants as owners) and M-15 (evidence), for the services you call. |
| `docs/contracts/11-ported-test-intent.md` | The "Grants" and "Writes" tables. |

## You own

`src/chamberlain/writes/`, `src/chamberlain/domain/grant_rules.py`,
`tests/writes/`, migrations `0030`-`0039`. Tables you alone write:
`grants`, `known_recipients`, `operations`, `audit_log`, and `writes.*`
system keys.

## Do not touch

Everything listed as shared in `docs/OWNERSHIP.md` (including
`domain/intents.py`, `domain/operations.py`, `domain/grants.py`, which you
build on), the other tracks' directories, the old repository and its data.
Do not import `chamberlain.memory`, `chamberlain.attention` or
`chamberlain.connectors`. You do not build the viewer UI or its HTTP
server; you build the confirmation endpoint logic and its contract tests.

## Build

1. `writes/repo.py`: all SQL for your tables.
2. `writes/audit.py`: the append-only hash chain, verification, the
   monthly checkpointed pruning (G-20 to G-23). Implement
   `app.services.AuditLog`.
3. `domain/grant_rules.py`: pure rules. Review anniversaries in the
   configured zone, clamped for short months and DST-stable (G-6); scope
   matching, exact-field (W-22); requirement selection (the G-12 table).
4. `writes/grants.py`: lifecycle with status changes only, immutability,
   supersession, suspension, card-bound revocation, compaction (G-2 to
   G-11). Usage derived from `operations` (G-11).
5. `writes/authorise.py`: the pure `authorise(intent, operation, grants,
   usage, policy, now) -> Decision` (W-20 to W-22), including send limits
   (G-16) and known recipients (G-15).
6. `writes/engine.py`: the state machine. Intent completion and hashing at
   propose (W-1, W-2), idempotency keys (W-15 to W-19), journal-before-
   dispatch (W-7), outcome mapping (W-10, W-11), reconcile with
   `SETTLE_AFTER` and proof (W-12), crash recovery exposed as
   `recover(ctx)` for the tick (W-9), verification upgrade (W-14), partial
   results (W-24), compaction (W-25). Every transition goes through
   `domain.operations.check_transition`, in one transaction with its audit
   row. Attachments are read through `ctx.services.artifacts` and checked
   by hash twice (W-3); locators come from `ctx.services.refs`.
7. `writes/confirm.py`: the confirmation channel as plain functions plus a
   WSGI-style request handler with no server of its own: token and origin
   checks, the seven endpoints of G-19, hash-echo on confirm, audit rows
   with actor `human:viewer` (G-17 to G-19). Wave 2 mounts it in the viewer.
8. `writes/services.py`: `build(...) -> (AuditLog, WriteControl)`.
9. `writes/handlers.py`: fill `HANDLERS` for `propose`, `do`, `reconcile`
   and `DOCTOR_CHECKS` with the audit-chain check.

Agent-facing wording matters here: a send that is only client-verified is
"submitted", never "delivered" (W-14). The preview in `propose` is
`Content`.

## Read in the old repository (read-only)

`monitor/approvals.py` (grant-anchored reviews, content hashes; note lines
197 and 243 delete rows, which you must not reproduce),
`monitor/adapters/thunderbird/commands.py` `write()` (lines 163-273: the
journal stages, the pre-send failure that releases the grant, reconcile
without reissue), `monitor/adapters/macos/operations.py`
(`require_approval`, `allowed`, `journal`), `check_send` in
`monitor/adapters/api.py`, and `monitor/viewer.py` for the loopback, token
and origin checks. Tests: `tests/test_approvals.py`,
`tests/test_thunderbird.py`, `tests/test_thunderbird_mail.py`,
`tests/test_viewer.py`, `tests/test_threadcache.py` (the pending-approval
cases). Port intent, not code; the old design let the agent write both the
evidence and the approval, which is the gap you are closing.

## Done means

All in `tests/writes/`, each naming its rule ID, all against the fake
provider (`conftest.py` gives you `fake`, `ctx`, `clock`) and the service
doubles:

- every row of the "Grants" and "Writes" tables in
  `11-ported-test-intent.md`;
- the three engine properties at the end of `04-writes.md`, as Hypothesis
  stateful tests over random propose, confirm, do, timeout, crash, restart
  and reconcile sequences with every `Fault` armed at random. Assert on
  `fake.dispatch_count(key) <= 1` for every key, ever;
- a table-driven test that every state pair not in `TRANSITIONS` is
  refused;
- `authorise` property tests: pure (same inputs, same decision), never
  allows on a non-active grant, never allows content that differs from a
  one-off's hash, never exceeds a limit;
- grant tests: never deleted (row count never falls on revoke, spend or
  expiry), immutable fields, review anniversaries across month ends and
  DST, suspended after the grace period, card-bound revocation;
- confirmation tests: no token or wrong origin is 403 and audited; confirm
  with a different hash is 409 and activates nothing; no tool call can
  activate a `human_confirmation` grant; the token never appears in any
  reply, log record or audit row;
- audit tests: the chain verifies; editing or deleting any row is
  detected; pruning leaves a verifying chain;
- an uncertain send followed by any sequence of `do` and `propose` calls
  never produces a second copy in the fake's sent folder (W-8, W-17);
- handler tests through `App.call` with golden replies;
- the gate is green.

## Report

Post a `HANDOFF` on the board: what exists, the gate result, the service
factory and `recover` signatures, the mount point for the confirmation
handler, the columns attention's detectors should read
(`grant.needed`, `grant.review_due`, `operation.uncertain`), anything in
the contract you had to interpret, and what is not done.
