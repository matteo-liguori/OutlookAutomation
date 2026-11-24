# Wave 1, track (d): the Thunderbird connector

You build the one route to real mail: a Python client, the bridge protocol
version 2, and the Thunderbird add-on. It implements `MailPort` and
`ContactsPort` and nothing else. This brief is self-contained. Read
`AGENTS.md`, then `docs/BOARD.md`, then the contracts below, before writing
code.

## Binding contracts

| File | What binds you |
| --- | --- |
| `docs/contracts/08-ports.md` | All of it (P-1 to P-18). This is your specification. |
| `docs/contracts/04-writes.md` | Intents and what the engine expects of `prepare`, `dispatch`, `reconcile` (W-2 to W-5, W-10, W-12, W-14, W-23, W-24). You do not implement the engine. |
| `docs/contracts/03-envelope-errors.md` | The error codes a connector may raise (E-10, E-15, P-18). |
| `docs/contracts/01-domain.md` | Layering D-10: no SQLite, no store, no grants, no guidance, no journal. |
| `docs/contracts/11-ported-test-intent.md` | The "Writes", "Read state and coverage" and "Bridge" tables. |
| `docs/contracts/12-bridge-v2.md` | You write this file first (see below); it then binds you and the add-on. |

## You own

`src/chamberlain/connectors/thunderbird/` including `addon/`,
`docs/contracts/12-bridge-v2.md`, `tests/thunderbird/`. No migrations: a
connector does not touch SQLite.

## Do not touch

Everything listed as shared in `docs/OWNERSHIP.md`, the other tracks'
directories, the old repository and its data. Do not import
`chamberlain.store`, `chamberlain.app`, `chamberlain.memory`,
`chamberlain.attention`, `chamberlain.writes` or `sqlite3`. **Never connect
to a real Thunderbird profile, mailbox or account in this wave**: no live
validation, no installing the add-on, no registering a native messaging
host on this machine. Everything is tested against a scripted transport.
Do not rebuild the calendar module (Owl) or anything for Outlook.

## Build

1. `docs/contracts/12-bridge-v2.md`, first, and post it as an `INTERFACE`
   entry. It fixes: the frame format (length prefix, size limits per
   direction, chunked binary transfer with hashes); a handshake carrying
   protocol version, add-on version and a capability list, with refusal on
   mismatch; a request ID on every message; the closed command list (reads:
   `status`, `accounts`, `folders`, `query`, `read`, `attachment`,
   `resolve`, `refresh`, `contacts`; writes: `send`, `draft_save`,
   `draft_update`, `draft_discard`, `move`, `trash`, `mark_read`; each
   write's read-only `reconcile_*` twin); typed results; the error codes
   and their mapping to `ErrorCode`; local authentication of clients of
   the native host; and what the add-on must never do (no journal, no
   retry, no `eval`, no network listener, no command outside the list).
2. `protocol.py`: every message as a Pydantic model; the JSON Schema they
   emit is the source for the add-on's generated TypeScript types.
3. `frames.py`: reader and writer for length-prefixed frames with limits.
4. `native_host.py` and `client.py`: the native messaging host and the
   local client, over an injectable transport so tests need no Thunderbird.
5. `mail.py`: `MailPort`. `contacts.py`: `ContactsPort`. Stable locators
   that survive a restart (P-6); honest coverage and freshness (P-4, P-5);
   reads that never change the unread flag (P-3); `prepare`, `dispatch`,
   `reconcile` exactly as P-9 to P-15, with no journal and no internal
   retry (P-10, P-13). `dispatch` returns `not_applied` only when the
   add-on proves the command was never issued; a timeout is `unknown`.
6. `addon/`: the add-on in TypeScript (or typed JavaScript checked with
   `tsc --checkJs` if a build step proves impractical; say which on the
   board), with types generated from `protocol.py`, a fixed command list,
   and unit tests runnable with Node. Port behaviour from the old
   `background.js`, `mail.js` and `client-api.js`, and delete the add-on's
   own `pilot:` journal: the engine owns idempotency now.
7. `setup.py`: build the versioned add-on package and write the native
   host manifest into a directory given as an argument. It must not
   register anything system-wide in this wave.
8. A `ThunderbirdRig` in `tests/thunderbird/` implementing `MailRig` over a
   scripted transport, so `MailPortContract` runs against your connector
   on every gate. Where the scripted transport cannot inject a fault,
   `inject` returns False and the test is skipped visibly.

Capabilities are declared `implemented`, never `live_verified`: live
validation is a later, separate step against a disposable account.
`absence_provable` for `mail.send` stays empty unless you can state, in the
bridge contract, exactly which folders are read and why that is complete.

## Read in the old repository (read-only)

`monitor/adapters/thunderbird/README.md` and `STATUS.md` first: they
record the incidents behind the odd-looking conditions. Then
`native_host.py` (frames, user-only socket, Windows ACL, client
authentication), `commands.py` (locators and `resolve_entry`,
`reconcile_move`, `settle_calendar_timeout` for the pattern only,
`mark_read`, `folder_id`), `mail.py`, `setup.py`, and the add-on:
`addon/background.js`, `addon/mail.js`, `addon/client-api.js`,
`addon/client-schema.json`, `addon/manifest.json`. Tests:
`tests/test_thunderbird.py`, `tests/test_thunderbird_mail.py`,
`tests/Test-ThunderbirdBridge.js`, `tests/Test-ThunderbirdMail.js`.

Every one-line condition there encodes a real incident (the bound Sent
Items folder, reply headers, a provider merging copies, a move finishing
before the header cache updates, session integers changing on restart).
Before porting, list them in your handoff as named behaviours, each with
the test that now covers it. Do not port the Mac or Windows Outlook
adapters, `calendar-api.js`, or the old `approval_id` plumbing.

## Done means

All in `tests/thunderbird/` (Python) and the add-on's own tests (Node),
each naming its rule ID:

- `MailPortContract` and `ContactsPortContract` pass against your rig, with
  any skipped fault listed in the handoff;
- every row of the "Bridge" table and the Thunderbird rows of the "Writes"
  and "Read state and coverage" tables in `11-ported-test-intent.md`;
- Hypothesis fuzzing of the frame reader (truncated, oversize, garbage,
  split across reads) and of protocol message parsing: it rejects cleanly
  and never raises anything but `ChamberlainError`;
- handshake tests: a version or capability mismatch refuses to operate and
  maps to `source.unavailable` with a fix naming the add-on update;
- an unauthenticated local client is refused; a dropped client never
  crashes the host;
- a command outside the closed list is refused by the host and by the
  add-on;
- the generated TypeScript types are up to date with `protocol.py` (a test
  regenerates and compares);
- the add-on type-checks and its tests pass. Post a `CONTRACT-CHANGE`
  entry proposing how the gate and CI should run them (a Node step in
  `dev.py` and the workflow); the architect makes that change;
- the layout test still passes (no forbidden imports);
- the gate is green.

## Report

Post a `HANDOFF` on the board: what exists, the gate result, the list of
ported behaviours with their tests, skipped contract tests and why, the
add-on build and test commands, what live validation will need to check
first, and what is not done.
