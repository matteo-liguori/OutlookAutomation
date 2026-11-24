# Chamberlain

A local mail and calendar harness for AI agents. The agent gets twelve typed
tools and a three-sentence prompt; the harness notices what needs attention,
hands over guidance when it is relevant, keeps every stored item owned and
time-limited, and refuses any write that lacks verifiable permission or
would be a blind resend.

This is the rebuild. The old harness stays as the fallback until you cut over.

- Why and how: [docs/rebuild-plan.md](docs/rebuild-plan.md)
- The binding specification (the contracts): [docs/contracts/](docs/contracts/00-index.md)
- The guidance rules, as one page for your review: [docs/guidance-review.md](docs/guidance-review.md)
- Who builds what: [docs/OWNERSHIP.md](docs/OWNERSHIP.md), [docs/waves/](docs/waves/)
- Message board for builders: [docs/BOARD.md](docs/BOARD.md)

## Getting started

Use a new, empty data root for Chamberlain, never the old one. Pass it with
`--data <folder>` or set `CHAMBERLAIN_DATA`. Commands below use the Windows path to
Python; on macOS or Linux use `.venv/bin/python`.

1. Install. Python 3.12 or later, and Node 22.13 or later for the Thunderbird add-on.

   ```
   python -m venv .venv
   .venv/Scripts/python -m pip install -e ".[dev]"
   ```

2. Thunderbird add-on. The setup command builds the add-on and writes the native host
   manifest into the folders you name. It registers nothing system-wide.

   ```
   .venv/Scripts/python -m chamberlain.connectors.thunderbird.setup --bridge-dir <data root>/bridge --manifest-dir <manifest folder>
   ```

   Then, with your approval, register that manifest for your platform and install the
   built package in Thunderbird (Add-ons Manager, Install Add-on From File). It has its
   own ID, so the old add-on can keep running beside it.

3. Calendars, if you use them. Each is a guided command that saves secrets only in the
   operating system's credential store.

   ```
   .venv/Scripts/python -m chamberlain.connectors.google_calendar.setup client
   .venv/Scripts/python -m chamberlain.connectors.google_calendar.setup link you@example.com
   .venv/Scripts/python -m chamberlain.connectors.caldav.setup credential   # iCloud
   .venv/Scripts/python -m chamberlain.connectors.caldav.setup register
   ```

4. Import your old data, if you have any:

   ```
   chamberlain --data <data root> import <copy of old data root>
   ```

   The import only reads the old folder and never modifies it. Import a copy taken while
   the old harness is closed. It writes `config/connections.yaml` (once, never
   overwritten), your cards, and the report at `<data root>/import/report.json`. Then run
   `chamberlain config validate`. The full steps are in [docs/cutover.md](docs/cutover.md);
   settings are in [docs/configuration.md](docs/configuration.md).

5. Viewer. `chamberlain viewer` opens a local page where you confirm grants, settle
   uncertain operations and read reports.

6. Scheduling. See [docs/scheduling.md](docs/scheduling.md) for the five-minute tick and
   the passes in `connections.yaml`.

Check the result with `chamberlain doctor --metrics`. Real-account checks before cutover
are in [docs/live-validation.md](docs/live-validation.md).

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
chamberlain --data ./scratch-data doctor
```

## Try it without real accounts

The demo source is a small synthetic mailbox, calendar and address book held in memory
(`example.invalid` addresses, every item marked `[DEMO]`). Nothing it does leaves the process.

```
.venv/Scripts/python -m chamberlain.surface.cli demo init ./demo-data
.venv/Scripts/python -m chamberlain.surface.cli --data ./demo-data start
```

`demo init` refuses a folder that is not empty. Replies from a demo installation carry
`"demo": true` in `coverage`, and `doctor` warns if demo and real sources are mixed.
Use `--data ./demo-data` for any tool, or point an agent's MCP server at it
(`python -m chamberlain.surface.mcp_server --data ./demo-data`).

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
  importer/     one-shot import from the old harness
  testing/      connector contract suites and rigs
```
