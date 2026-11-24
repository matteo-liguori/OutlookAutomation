# Wave 1, track (b): the attention system

You build principle 1: the agent knows only that the harness exists and how
to call it; the harness notices what is relevant and hands over just enough
guidance at that moment. This brief is self-contained. Read `AGENTS.md`,
then `docs/BOARD.md`, then the contracts below, before writing code.

## Binding contracts

| File | What binds you |
| --- | --- |
| `docs/contracts/07-attention.md` | All of it (A-1 to A-19). This is your specification. |
| `docs/contracts/02-tools.md` | The standing prompt (T-1), `start`, `next`, `guide` (T-7), sessions (T-3), ticks (T-5). |
| `docs/contracts/03-envelope-errors.md` | `next`, `guidance`, `notices` (E-4 to E-6, E-8), the reply cap (E-2). |
| `docs/contracts/01-domain.md` | Language (D-1), handles, time, layering. |
| `docs/contracts/09-storage.md` | Your tables, one-writer rule, migrations `0020`-`0029`. |
| `docs/contracts/10-targets.md` | Principle 1 targets: sizes, the 90 percent trigger share. |
| `docs/contracts/04-writes.md`, `05-grants.md`, `06-memory.md` | Read for the states your detectors observe (W-9, W-13, G-8, M-26, M-27). You do not implement them. |
| `docs/contracts/11-ported-test-intent.md` | The "Attention" table. |

## You own

`src/chamberlain/attention/`, `src/chamberlain/guidance/` (the rule files),
`tests/attention/`, `tests/guidance/`, migrations `0020`-`0029`. Tables you
alone write: `sessions`, `events`, `guidance_deliveries`, `source_health`,
and `attention.*` system keys.

## Do not touch

Everything listed as shared in `docs/OWNERSHIP.md`, the other tracks'
directories, the old repository and its data. Do not import
`chamberlain.memory`, `chamberlain.writes` or `chamberlain.connectors`.
Detectors read other tracks' tables with `SELECT` only (A-8).

## Build

1. `attention/repo.py`: all SQL for your tables.
2. Sessions: start, resume, supersede, idle end, clean-up of session rows
   (A-10).
3. The inbox: upsert by dedupe key, expiry when a condition clears,
   handled, quiet periods, the not-agent-handleable kinds (A-1 to A-4).
4. Detectors, one pure function per event kind (A-5), over
   `DetectorInput`. Build now those that need only the store or the fake:
   `action.due`, `reminder.due`, `card.review_due`, `grant.needed`,
   `grant.review_due`, `operation.uncertain`, `source.stale`,
   `source.sign_in_needed`, `storage.over_budget`, `storage.orphans`,
   `mail.new_for_card`, `mail.new_unmatched`. Leave `calendar.conflict`,
   `user.note`, `update.available` and `schedule.pass_due` as registered
   detectors that return nothing, with a note for wave 2.
5. The tick (A-6, A-7): bounded read-only source deltas through
   `ctx.sources` (P-3), detectors, inbox reconciliation, then the hooks for
   the sweeper and for crashed-operation recovery. Those two belong to
   other tracks: define `TickStep = Callable[[Context], None]` and a list
   `EXTRA_STEPS` that integration fills; never import the other tracks.
6. The rule format: loader and validator for `guidance/<id>.yaml`, content
   hash, private override patches (A-11 to A-14, A-18).
7. Selection and budgets (the six steps, A-15 to A-17) and the decorator
   (A-19): `attention/hooks.py` implementing `app.context.Hooks`, exported
   as `HOOKS` from `attention/handlers.py`.
8. `attention/handlers.py`: fill `HANDLERS` for `start`, `next`, `guide`
   and `DOCTOR_CHECKS` with guidance checks (sizes, stale overrides).
9. The rules themselves. Rewrite the old 40 topics as short triggered
   rules with all syntax removed. The owner will read and approve them, so
   write them plainly and keep a table in your handoff mapping old topic to
   new rule IDs, or to "dropped: generated from the schema" or "dropped:
   legacy backend".

## Read in the old repository (read-only)

`monitor/guidance.py` (once-per-session delivery, `context_prompts`),
`monitor/briefing.py`, `defaults/behaviour.yaml` (the 40 topics; 34,584
characters to bring under 12,000), `monitor/scheduled_guard.py`,
`monitor/followups.py`, `monitor/reviews.py`, the prompt functions
`approvals.sending_notice`, `cards.upkeep_prompt`,
`improvements.review_prompt`, `maintenance.storage_prompt`. Tests:
`tests/test_guidance.py`, `test_scheduled_guard.py`, `test_followups.py`,
`test_card_reminders.py`, `test_reliability.py`. Do not read or copy the
owner's private `customisations.yaml` into this tree. Do not carry over
command syntax, the `help` verb or backend vocabulary.

## Done means

All in `tests/attention/` and `tests/guidance/`, each naming its rule ID:

- every row of the "Attention" table in `11-ported-test-intent.md`;
- inbox property tests (Hypothesis): random detect, deliver, handle and
  condition-clears sequences never give two live events for one key, never
  lose a true condition, and respect quiet periods (A-1 to A-4);
- scenario tests: given this store state and this tool call, exactly these
  rule IDs and notices, in this order (A-15), for at least: an uncertain
  send, a grant needing human confirmation, incomplete coverage, a
  storage budget passed, a first `start`, a scheduled `start`;
- budget tests in the gate: each rule within `max_chars` and 600; all rules
  within 12,000; reply guidance within 800; pointers used when over; over
  90 percent of rules have a trigger (A-12, A-13);
- content tests: no rule contains syntax, field names or forbidden words
  (A-11, A-14); the standing prompt is under 400 characters (T-1);
- decorator tests: it changes only `next`, `guidance`, `notices`, and keeps
  the reply under `MAX_REPLY_CHARS` (A-19);
- a failing detector or source never fails a tool call (A-7);
- handler tests through `App.call` for `start`, `next`, `guide`, with
  golden replies; `start` returns at most 5 items and no bodies (T-7);
- the gate is green.

## Report

Post a `HANDOFF` on the board: what exists, the gate result, total guidance
characters, the old-topic-to-rule table, the `EXTRA_STEPS` hook signature,
the columns your detectors read from other tracks, and what is not done.
