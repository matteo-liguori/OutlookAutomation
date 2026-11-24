# Chamberlain

A local mail and calendar harness for AI agents. The agent gets twelve typed
tools and a three-sentence prompt; the harness notices what needs attention,
hands over guidance when it is relevant, keeps every stored item owned and
time-limited, and refuses any write that lacks verifiable permission or
would be a blind resend.

This is the rebuild (alpha). It is being built in waves against a written
contract. Nothing here connects to a real mailbox yet.

- Why and how: [docs/rebuild-plan.md](docs/rebuild-plan.md)
- The binding specification: [docs/contracts/](docs/contracts/00-index.md)
- Who builds what: [docs/OWNERSHIP.md](docs/OWNERSHIP.md), [docs/waves/](docs/waves/)
- Message board for builders: [docs/BOARD.md](docs/BOARD.md)

## Set up

Python 3.12 or later.

```
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"      # Windows
.venv/bin/python -m pip install -e ".[dev]"          # macOS, Linux
```

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

Most tools answer `tool.not_implemented` until wave 1 lands.

## Layout

```
src/chamberlain/
  schema/       reply envelope, errors, the twelve tool shapes
  domain/       pure types and rules
  ports/        MailPort, CalendarPort, ContactsPort, Clock
  store/        SQLite connection and numbered migrations
  app/          the one entry point and the seams between tracks
  memory/       principle 2: records, artifacts, the sweeper        (wave 1)
  attention/    principle 1: events, guidance selection             (wave 1)
  writes/       authorisation, the write state machine, audit       (wave 1)
  connectors/   fake/ (done), thunderbird/ (wave 1), calendars (wave 2)
  surface/      cli.py, mcp_server.py, viewer/ (wave 2)
  guidance/     one small rule file per rule                        (wave 1)
  importer/     one-shot import from the old harness                (wave 2)
  testing/      connector contract suites and rigs
```
