# Chamberlain contracts

These files are the binding specification for the rebuild. The plan
(`docs/rebuild-plan.md`) explains why; the contracts say what. Where they
disagree, the contracts win and the discrepancy should be fixed alongside its regression test.

| File | Area |
| --- | --- |
| `01-domain.md` | Language, nouns, handles, time, layering |
| `02-tools.md` | The twelve agent-facing tools and their shapes |
| `03-envelope-errors.md` | Reply envelope, content marking, error taxonomy |
| `04-writes.md` | The write-operation state machine |
| `05-grants.md` | Grants, human confirmation, send limits, audit log |
| `06-memory.md` | Data classes, lifetimes, artifacts, sweeper, caps |
| `07-attention.md` | Events, detectors, guidance rules, budgets |
| `08-ports.md` | MailPort, CalendarPort, ContactsPort, capabilities |
| `09-storage.md` | SQLite schema, table ownership, migrations |
| `10-targets.md` | Numeric success targets |
| `12-bridge-v2.md` | Thunderbird bridge protocol |

## Reading rules

- MUST, MUST NOT, SHOULD and MAY carry their RFC 2119 meanings.
- Every normative rule has an ID (`W-3`, `G-7`, `M-12`). Tests that enforce a
  rule name its ID in the test name or docstring, so coverage of the contract
  can be searched for.
- The Python in `src/chamberlain/schema/`, `src/chamberlain/ports/` and
  `src/chamberlain/store/migrations/` is the machine-readable form of these
  files. When prose and code disagree, resolve the discrepancy against the owner ruling.
- Numbers that appear in more than one place are defined once in
  `src/chamberlain/limits.py` and quoted here.

## Changing a contract

Change the contract, implementation and proving tests together. Keep the local gate green.

## Scope fixed in phase A

- Twelve tools. Adding a thirteenth is a contract change that needs the
  owner's approval, not a builder's.
- Not rebuilt: Outlook COM, Mac Outlook, Gmail API, Microsoft Graph, screen
  automation, any language model inside the harness, any background service.
- Python 3.12+, Pydantic v2, SQLite, plain SQL.
