# Wave 2, track (f): viewer, runs and reports

You build the person's window into the harness and the clock that drives it. The viewer
is the only place a human confirms grants, so it is a safety component as much as a
dashboard. Read `AGENTS.md`, `docs/BOARD.md`, then the contracts below.

## Binding contracts

| File | What binds you |
| --- | --- |
| `docs/contracts/05-grants.md` | The confirmation channel (G-12 to G-19): exact content shown, hash-checked confirm, token, origin and host checks. |
| `docs/contracts/07-attention.md` | `schedule.pass_due`, `user.note`, `update.available` events; sessions per agent and purpose (R10). |
| `docs/contracts/06-memory.md` | Read-only views of cards, actions, storage figures. |
| `docs/contracts/10-targets.md` | `doctor --metrics` keys you surface. |
| `docs/contracts/09-storage.md` | Migrations `0050`-`0059`. |

## You own

`src/chamberlain/surface/viewer/`, `src/chamberlain/runs/` (new), `tests/viewer/`,
`tests/runs/`, migrations `0050`-`0059`.

## Build

1. A loopback-only local web viewer (bind 127.0.0.1, random per-launch token, Origin and
   Host checks, no third-party assets) that mounts the writes track's confirmation
   handler and shows: pending grants with the exact content to be authorised, grant
   review and revocation, read-only uncertain operations in the Timeline, "what the
   assistant did today" from the audit log, cards and due actions, storage figures.
   Port the safety checks from the old `monitor/ui/__init__.py`.
2. Runs: scheduled mail and calendar passes as `schedule.pass_due` events; the
   schedule record; `chamberlain tick` for the operating system scheduler (Windows Task
   Scheduler and launchd examples); the wrong-day stop rule (R10).
3. Reports: a run's written output stored as an owned artifact with a lifetime, shown
   in the viewer; no report is a loose file.
4. `user.note` events from the viewer and `update.available` from the tag checker.

## Done means

The gate passes; viewer tests cover token, origin and host refusal, the hash-checked
confirm (409 on mismatch), and that nothing the agent can call activates a human-only
grant; run tests cover the wrong-day stop and a scheduled pass producing one report
artifact that the sweeper later removes.

## Do not touch

Shared files without a board entry, other tracks' directories, the old repository and
the owner's data.
