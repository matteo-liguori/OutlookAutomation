# Chamberlain

A local mail and calendar harness for AI agents. The agent gets twelve typed
tools and a three-sentence prompt; the harness notices what needs attention,
hands over guidance when it is relevant, keeps every stored item owned and
time-limited, and refuses any write that lacks verifiable permission or
would be a blind resend.

**Version 2 is a rebuild.** It replaces the first version (Outlook Automation), which now lives
on the [`v1-legacy`](https://github.com/matteo-liguori/chamberlain/tree/v1-legacy) branch. Your
v1 data comes across with one agent-guided migration (see [Coming from v1](#coming-from-v1)). [Why version 2](#why-version-2) explains what changed.

## Why version 2

Version 2 is built on three principles.

### 1. Far, far safer

Nothing changes your mail or calendar without authority you can check:

- **Permission.** Every send, move, trash, mark-read and calendar change needs a permission you
  confirm yourself in the local viewer. The agent can propose one but never confirm it.
- **Exactly once.** Each write is recorded before it is sent and checked afterwards. A write whose
  outcome is unclear is settled by reading the mailbox or calendar, never by sending it again. A
  near-duplicate of a recent message is refused unless you say otherwise.
- **Unread stays unread.** Reading or filing mail leaves it unread unless you have chosen
  otherwise.
- **Audit.** Every write and every permission is recorded in an append-only, hash-chained audit
  log that the harness verifies.
- **Off limits.** Passwords, payments and accepting terms are refused whatever is granted.
  Secrets live only in the operating system's credential store, and the data folder may never sit
  inside the code checkout.

### 2. Integrated, streamlined and token-efficient

The harness does the bookkeeping, so the agent spends its tokens on your mail, not on the tool:

- **A small interface.** Twelve typed tools and a three-sentence prompt replace v1's large command
  set and long instructions. They work from the command line or as an MCP server, so Claude, Codex
  or any other agent gets the same rules.
- **The harness does the noticing.** Each pass or chat starts with a short list of what needs
  attention: new mail per card, due actions and reminders, calendar conflicts, sources that stopped
  answering. Guidance arrives only when it applies, within fixed size budgets.
- **Bulk, not one at a time.** Filing, trashing and marking read move many messages in one write
  and one check.
- **One harness everywhere.** Thunderbird carries Gmail, Microsoft 365 and IMAP mail on Windows and
  Mac. Google Calendar and iCloud are written directly; Microsoft 365 calendars go through
  Thunderbird.
- **One place to look.** The local viewer shows each pass's report, a calendar across every
  account, the permissions waiting for you, and reminders you acknowledge yourself.
- **Bounded storage.** Everything kept has an owner and an expiry, and a sweeper enforces both.

### 3. Robustly tested, self-auditing, on fully supported tools

- **Supported foundations.** v1 drove Classic Outlook through COM. Microsoft is replacing Classic
  Outlook with the new Outlook for Windows, which has no COM automation, and Outlook on the Mac
  offers little to automate. v2 rests on supported, documented interfaces instead: Thunderbird's
  extension APIs, the Google Calendar API and the CalDAV standard.
- **Tested in depth.** About 1,060 automated tests run on every change. They include contract
  suites that hold the test doubles and the real Thunderbird add-on to the same promises, property
  tests of the write state machine, and a checklist that maps each capability to the test proving
  it.
- **Tested for real.** `chamberlain live-test` exercises every capability end to end against your
  own mailboxes and calendars: sends, replies, drafts, attachments, filing, trash and restore, lost
  replies and calendar edits. It cleans up everything it made and reports a score.
- **It checks itself.** `chamberlain doctor` verifies the audit chain, the schema, storage and the
  health of every source. Sync gaps and stale sources are reported, never hidden. It can also
  review how agents actually use the harness.

### Also new

- Mail clean-up rules, off by default.
- Conversation export: one zip per thread, with each attachment stored once.
- Update notices when a new version is tagged.

## Coming from v1

Give your agent the [agent setup guide](docs/setup-agent.md). It walks the agent through:

- installing;
- migrating your v1 data, with a backup, a resumable import and a receipt;
- binding your accounts in Thunderbird;
- a supervised first pass.

It asks you only for the steps that need you, such as signing in or approving a permission.
Cards, actions, grants, evidence, your instructions and your report layout all come across. Your
v1 folder is only read, never changed, and it stays the fallback until you switch:
[docs/cutover.md](docs/cutover.md).

## More

- Why and how: [docs/rebuild-plan.md](docs/rebuild-plan.md)
- The binding specification (the contracts): [docs/contracts/](docs/contracts/00-index.md)
- When you need to click, standing permissions, known recipients and authorised senders: [docs/permissions.md](docs/permissions.md)
- The guidance rules, as one page for your review: [docs/guidance-review.md](docs/guidance-review.md)
- Capability preservation and proof: [docs/capability-checklist.md](docs/capability-checklist.md)
- Working with an agent: [docs/agents.md](docs/agents.md)

## Getting started

Follow the [agent setup guide](docs/setup-agent.md) for a new installation or migration. The command `chamberlain setup status` shows where to resume.

Use a new, empty data root for Chamberlain, never the old one. Pass it with
`--data <folder>` or set `CHAMBERLAIN_DATA`. The folder must be outside the code checkout:
Chamberlain refuses to start when the data root is inside the folder that holds the code or
inside its Git work tree, so private mail can never be committed by accident. Commands below use the Windows path to
Python; on macOS or Linux use `.venv/bin/python`.

1. Install. Python 3.12 or later, and Node 22.13 or later for the Thunderbird add-on.

   ```
   python -m venv .venv
   .venv/Scripts/python -m pip install -e ".[dev]"
   ```

2. Thunderbird add-on. The setup command builds the add-on and writes the native host
   manifest. With `--register` it shows how it would point Thunderbird at the manifest (the
   Windows registry value, or the Mac manifest folder); add `--apply` to do it.

   ```
   chamberlain --data <data root> setup thunderbird --register
   chamberlain --data <data root> setup thunderbird --register --apply
   ```

   Then install the built package in Thunderbird (Add-ons Manager, Install Add-on From File).
   It has its own ID, so the old add-on can keep running beside it. Setup steps can close and
   start Thunderbird with `chamberlain thunderbird close|start|status`.

   To update the add-on later, never use the Add-ons Manager: run
   `chamberlain addon update --restart`. It builds the package, closes Thunderbird normally,
   replaces the add-on file in every profile that has it, and starts Thunderbird again.

3. Calendars, if you use them. Each is a guided command that saves secrets only in the
   operating system's credential store.

   ```
   .venv/Scripts/python -m chamberlain.connectors.google_calendar.setup client
   .venv/Scripts/python -m chamberlain.connectors.google_calendar.setup link you@example.com
   .venv/Scripts/python -m chamberlain.connectors.caldav.setup credential   # iCloud
   .venv/Scripts/python -m chamberlain.connectors.caldav.setup register
   ```

   Upgrading from the old harness? Skip these: the import (step 4) moves the saved Google and
   iCloud secrets and connects the calendars.

4. Import your old data, if you have any:

   ```
   chamberlain --data <data root> import <copy of old data root>
   ```

   The import only reads the old folder and never modifies it. Import a copy taken while
   the old harness is closed. It writes `config/connections.yaml` (once, never
   overwritten), your cards, and the report at `<data root>/import/report.json`. The long
   free-text context the old harness kept on each open card is stored with its card as a
   note called "context from the old harness". The card is marked due for review, and the
   assistant folds what still matters into the card and forgets the note. Unread notes
   expire after 60 days. Running the import again adds this context to cards that were
   imported before it was carried over, and never twice. Then run
   `chamberlain config validate`. The full steps are in [docs/cutover.md](docs/cutover.md);
   settings are in [docs/configuration.md](docs/configuration.md).

5. Viewer. `chamberlain viewer` opens a local page where you confirm grants, answer requests for larger cards (see
   [docs/permissions.md](docs/permissions.md#larger-cards)) and read the reports. It opens on the
   calendar, with today's report below it (see [The report](#the-report)). The page keeps
   its token for the tab, so a reload works; a new browser session needs the token link again.
   The viewer opens as its own app window (Chrome, else Edge, with a separate profile), with no tabs or
   address bar. It is opened once and reused: a later start, or a reply held for you, shows in that
   window instead of opening another.
   The theme switch in the header offers Match system (the default), Light and Dark; the choice is kept in that window's browser storage.

6. Scheduling. `chamberlain schedule install --apply` registers the hourly tick (plus one run
   before each pass), and
   `chamberlain schedule prompts` prints the agent automations for the passes in
   `connections.yaml`. Connect your agent with `chamberlain setup agent codex|claude --apply`.
   See [docs/scheduling.md](docs/scheduling.md) and [docs/agents.md](docs/agents.md).

Check the result with `chamberlain doctor --metrics`. Real-account checks before cutover
are in [docs/live-validation.md](docs/live-validation.md).


## Exporting a conversation

`chamberlain export thread <Message-ID> [<Message-ID> ...]` (or `--file ids.txt`, one per
line) writes one zip to `<data root>/exports/`: `conversation.txt` with every message once,
in date order, everyone in the conversation listed at the top, quoted history trimmed
(`--keep-quotes` keeps it), and each attachment stored once under `attachments/` and tagged
`[A001]` wherever it was sent. `manifest.json` lists the messages, the attachment hashes and
any Message-ID not found. Messages are looked up in every configured mailbox and only read.
The cleaner deletes exports after a week: copy the zip elsewhere to keep it.
## The report

The written report of each scheduled pass is on the viewer's front page, the Calendar: today's
reports sit below the calendar, and any earlier day's open from the date list or by clicking a
day. The Schedule tab lists the pass times and each recent pass's state.

- **Who writes what.** The harness writes the sections it can prove from its own records, when the
  pass ends: what waits for you (orange, at the top: every open action marked for you, oldest
  first), access problems per source (red, labelled), important appointments (orange,
  labelled), permission reviews that are due, and a verification table (newest mail, how far mail
  was read, calendar freshness, filing counts, and **Partially verified** when anything is short).
  The assistant writes only the rest, usually the status of each item and what you must do next.
  It is told when its report is due and given an outline of its sections; it keeps the text under
  the run, and the harness never lets that text pose as a red or orange block.
- **The live report.** The newest pass's report stays live until the next pass starts (the
  viewer says "Live: updates until the next pass"). Its harness sections show the current state,
  and when you and the assistant settle something in a chat, it rewrites its part; the newest
  text replaces the old. When the next pass starts, the report is frozen as it stands.
- **Your layout.** The standard layout is `src/chamberlain/runs/report_default.yaml`. Your own is
  one private file, `config/report-template.yaml` in the data root; `chamberlain import` writes it
  from your old report template (once, never over an existing file). It names the sections, their
  order, headings and styles, and the wording the assistant gets for its own sections. A layout
  without the "Waiting for you" section gets it at the top.
- **Calendar.** Events show in the colour of their category (work, personal, social, travel,
  health, occasion, home; grey until the assistant gives one, with a short title if the invite is
  vague). **Add local event** keeps an event only in the viewer; no calendar is changed.
- **Important appointments.** The assistant decides which appointments are important (from your
  card rules and the context) and sets reminders for them. A due reminder opens by
  itself when it is due. Hold the button for five seconds (pointer, Space or Enter) to acknowledge
  it. That finishes the reminder and the audit log records you, not the assistant.

## Live test

`chamberlain live-test` checks the whole harness against your real mailboxes and calendars,
using the same tools an agent uses. It is off until you switch it on and list the sources in
`live_test` (see [docs/configuration.md](docs/configuration.md)). Full facts:
[docs/live-test.md](docs/live-test.md).

```
chamberlain live-test grant                  # once, then confirm the grants in the viewer
chamberlain live-test run                    # everything that can run
chamberlain live-test run --dry-run          # show the plan; touch nothing
chamberlain live-test run --approve-calendar-edits
chamberlain live-test sweep                  # remove marked items left by an earlier run
``` Several reminders for one date (a month, three weeks and a week before) are one date: the
  calendar shows only real events (the matching event is highlighted in gold), each reminder day
  brings one pop-up, and acknowledging it clears every reminder due so far.

- **What it does.** Sends marked mail between accounts you own, files, marks and trashes it,
  saves drafts, makes marked calendar events, removes everything it made, and gives a score.
  It sends only to the addresses you list.
- **The marker.** Every message subject and event title it makes starts with
  `CHAMBERLAIN LIVE TEST <run id>`. Cleanup and `sweep` touch only items with that marker.
- **No send cap.** It sends only to your own addresses, and the hourly send cap does not apply
  to mail only to you.
- **Calendar edits.** A standing grant cannot cover them. Pass `--approve-calendar-edits` for one
  run, or set `live_test.approve_calendar_edits: true` in `connections.yaml`. Without either,
  the calendar scenarios are skipped and say why.
- **The report.** Each run writes `logs/live-test/<run id>.json` and `.md` in the data root (handles,
  error codes, counts and timings; never subjects, bodies or addresses). `chamberlain live-test
  report` prints the latest again, and the viewer lists recent runs.

## Development setup

The type checker (pyright) fetches its engine through Node the first time it
runs; Node 18 or later must be on the path.

## The gate

One command, which must pass before any work is reported or merged:

```
.venv/Scripts/python -m chamberlain.dev check        # Windows
.venv/bin/python -m chamberlain.dev check            # macOS, Linux
```

It runs the format check, the linter, the type checker and the tests.
`python -m chamberlain.dev fix` applies the formatter and safe lint fixes.

## Try it

```
chamberlain tools                      # the twelve tools
chamberlain schema propose             # a tool's request and response schema
chamberlain prompt                     # the standing prompt an agent is given
chamberlain --data ../chamberlain-scratch doctor
```

## Layout

```
src/chamberlain/
  schema/       reply envelope, errors, the twelve tool shapes
  domain/       pure types and rules
  ports/        MailPort, CalendarPort, ContactsPort, Clock
  store/        SQLite connection and numbered migrations
  app/          the one entry point and the seams between tracks
  memory/       principle 2: records, artifacts, the sweeper
  attention/    principle 1: events, guidance selection
  writes/       authorisation, the write state machine, audit
  connectors/   fake/, thunderbird/, calendars
  surface/      cli.py, mcp_server.py, viewer/
  guidance/     one small rule file per rule
  importer/     resumable migration from v1
  runs/         scheduled passes, reports, update checks
  connections/  the connection profile, setup and onboarding
  cleanup/      the mail clean-up rules
  export/       conversation export
  review/       review of how agents use the harness
  livetest/     the live test
tests/support/  connector contract suites and rigs
```
