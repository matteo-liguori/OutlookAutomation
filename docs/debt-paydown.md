# Paying down the complexity debt (plan, 2026-11-25)

From three read-only scouts of v2: write path, connectors, and features/surfaces. Each wave
leaves the gate green and is committed on its own. Behaviour must not change except where a
bug is named. Run targeted live scenarios after any wave that touches connectors or the add-on.

Already done today: every operation on many mails is batched. Trash, filing and mark-read each
take one write, one Thunderbird call and one check. The clean-up scan, live-test clean-up and
export no longer work per message.

## Wave 0: real bugs (small, do first)

1. `GrantService.activate`: the lapse is rolled back. `raise` inside `with transaction` undoes
   `_end(grant, LAPSED)` and its audit row (writes/grants.py ~171).
2. `has_attachments` is always false in search results. `header()` in the add-on hard-codes it
   (reads.ts ~41), and `find` shows it.
3. Owl calendar health may never be OK. `health` needs `fresh_as_of`, and nothing sets it
   (connectors/thunderbird/calendar.py ~145). Check live first.
4. `read_state.propose_missing` builds `WritePolicy()`, so grant review dates use UTC instead
   of the owner's zone.
5. `chamberlain cleanup` opens the app outside its try block, so a config error comes out as a
   traceback.

## Wave 1: one home for small scattered concerns (mechanical, low risk)

- **Time.** One `domain/timefmt.py` (`ts`, `parse`, `now_ts(clock)`) replaces about 30 inline
  `"%Y-%m-%dT%H:%M:%SZ"` uses. Delete `memory/timeutil.py`, `livetest/timeutil.py`, `format_ts`,
  `parse_ts`, `stamp`, and the private `_fmt`/`_parse` pairs.
- **Actors.** `domain/actors.py`: `TICK`, `VIEWER`, `agent(session)`. Replaces about 20 literals and 5
  copies of `ACTOR`.
- **Message-IDs and subjects.** `domain/mail.py` gets `bare_id()` and `strip_reply_prefix()`. They
  replace six `bare` variants and two diverging reply-prefix regexes. Add shared test vectors
  with the add-on.
- **Paging.** `ports/page.py` gets `iter_pages()` and `collect_all()`, returning items and whether
  the result is complete. They replace about 10 hand-written cursor loops, which today treat
  gaps differently.
- **Ports.** `SourceRouter.port(source)` and `write_port(intent)` replace three hand-rolled
  mail/calendar/contacts dispatches and the parallel `_CALENDAR_KINDS`/`_CALENDAR_INTENTS`.
- **Attachments.** One `attachments_of(intent)` replaces 9 `getattr(x, "attachments", ())`.
- **Calendar connectors.** `calendar_support/common.py` takes the shared `_text`, `_why`,
  `_unavailable`, `_PROVABLE`, partstat maps and one cursor/locator codec.
- **Timing.** Timeouts and deadlines go in one generated constants module shared with the add-on.

## Wave 2: CLI, MCP and the tool client (low risk)

- One `with_data_root` plus `run_command` runner for `cli.py` and `mcp_server.py`. This gives one error
  output form, and the handler if-chain becomes a table.
- A shared `app/client.py` `ToolClient` (start, call, raise, `ensure_card`). It replaces
  `cleanup._Session`, `Lab.call`/`ok` and `livetest.authority.ensure_card`.
- The MCP server keeps one app per process instead of building one per call.
- Config comes in on `Context`: zone, mail sync and the write policy. Delete `configured_zone`, which
  re-reads YAML on every start, and the policy plumbing passed through about 10 signatures.

## Wave 3: the write engine (medium risk; the main readability win)

- Per-kind logic moves onto the intents or into one table per concern: locate, complete,
  scope, `describe`, and after-confirm. Add a Cover flag for the folder-match rule, so
  `scope_mismatch` stops special-casing kinds.
- An `Engine._move(op, to, summary, **cols)` helper with a typed patch replaces 15 repeated
  `move_operation(...)` calls.
- Typed row accessors (`op.intent()`, `.refs()`, `.receipt()`) and one `parse_intent()`.
  `confirm` and the viewer then stop importing `Engine`.
- A typed `OperationFacts` replaces the magic underscore keys in `refs` (`_allow_duplicate` and
  similar). Delete the "older store" compatibility cases.
- Then split `engine.py` (about 1,980 lines) into propose / dispatch / reconcile / resend / outside.

## Wave 4: Thunderbird connector and add-on (medium risk; live test after)

- One `CommandSpec` table drives the `Command` enum, the args and result models, the write set,
  typegen, and a typed TS handler map. `client.call` returns typed results.
- A plan discriminator or per-kind handler classes in `mail.py` replace the
  `prepare`/`dispatch`/`reconcile` ladders and the `batch` sniffing. Then split the file.
- Delete dead protocol surface: `retryable`, unused header fields, constant result fields,
  `QueryArgs.text`, the `ACCOUNTS` command, `refresh` flags, `timing_ms`, and empty args subclasses.
- Make the add-on the single "same message?" verifier. Pass the fingerprint in, and drop the
  Python `_duplicate_scan` and its per-header reads.
- Keep one bridge connection per prepare/reconcile sequence instead of a handshake per command.
- `attachment()` stops doing a full `read()` plus raw hash. The raw hash becomes opt-in.
- `syncFolders` runs in parallel, which lifts the batch-of-3 tuning.
- Drop the Owl calendar adapter layer and use `BridgeClient` and typed models directly.

## Wave 5: features (medium risk)

- Split `attention/watermark.scan` (about 220 lines) into read, plan and commit phases.
- Stop cross-track table access. `runs/read_state` subclasses `Engine` and writes to
  attention's `mail_seen`, and `attention/repo.py` holds about 220 lines of foreign SQL. Expose
  small services instead.
- `find`: one `_over_sources` helper for the soft-failure and gap logic. Add `remember_many` for hits.
- Delete the retired `new_for_card` detector and its event kind.
- Move `testing/` and `app/doubles.py` out of the package into `tests/support/`.

## After cutover only (needs the owner)

- Delete the importer and its hooks: about 2,750 lines plus tests, `intent_json IS NULL`
  operations and the imported-card code. Cutover still needs it.
- Squash the 17 migrations into one baseline. The live store must be rebuilt once.
- Trim the build-time docs (`docs/waves`, `BOARD.md`, `rebuild-plan.md`) and the "wave 2"
  names in code.
