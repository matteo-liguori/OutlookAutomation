# 01 Domain language

Binding for every track. Code: `src/chamberlain/domain/`, `src/chamberlain/limits.py`.

## Nouns

| Term | Meaning | Agent sees it |
| --- | --- | --- |
| Source | One enabled mailbox, calendar or address book. ID `mail:work`, `calendar:home`, `contacts:work`. | Yes |
| Connector | The code path that reaches a source. | Never |
| Card | A tracked matter. Replaces case and thread. | Yes |
| Action | One open obligation on a card, with a due time. | Yes |
| Note | Scratch text scoped to one action. Dies with it. | Yes |
| Evidence | A small immutable record of what was said or observed. | Yes |
| Artifact | Any stored body, attachment, download or file, addressed by a handle. | Yes |
| Grant | The user's permission: one-off (exact content) or standing (scope and limits). | Yes |
| Operation | One attempted write and its lifecycle. | Yes |
| Event | Something the harness detected that may need attention. | Yes, as work items and notices |
| Guidance rule | A short instruction with a trigger. | Yes, as delivered text |
| Session | One agent conversation with the harness. | Yes, as a handle |
| Ref | The harness's stable pointer to a remote message, event or contact. | Yes, as a handle |

- **D-1** Agent-facing text, field names and error messages MUST use only
  these nouns. The words `outlook`, `monitor`, `pilot`, `case`, `thread`,
  `approval`, `EntryId`, `snapshot` and any connector name MUST NOT appear in
  a tool reply. A test greps tool schemas and guidance for them.
- **D-2** Agent-facing field names MUST be `snake_case`.

## Handles

A handle is `<prefix>_<token>`: a fixed prefix, an underscore and 8 to 32
characters of `[a-z0-9]`. Prefixes:

| Prefix | Kind | Minted by |
| --- | --- | --- |
| `card` | Card | memory |
| `act` | Action | memory |
| `note` | Note | memory |
| `evd` | Evidence | memory |
| `art` | Artifact | memory |
| `grant` | Grant | writes |
| `op` | Operation | writes |
| `evt` | Event | attention |
| `ses` | Session | attention |
| `msg` | Mail message ref | app (refs table) |
| `cev` | Calendar event ref | app (refs table) |
| `con` | Contact ref | app (refs table) |

- **D-3** Handles are opaque to the agent. They MUST NOT encode file paths,
  connector locators, mailbox names or content.
- **D-4** Source IDs are `<kind>:<name>` with kind in `mail`, `calendar`,
  `contacts` and name `[a-z0-9-]{1,32}`. They are not handles and are safe to
  show.
- **D-5** A `msg`, `cev` or `con` handle maps, in the `refs` table, to a
  source ID and a connector locator. The locator MUST be the connector's
  stable form (one that survives a client restart, see P-6), never a
  session-scoped integer.
- **D-6** A ref handle is deterministic: the same source and stable locator
  always yield the same handle (`domain.handles.ref_handle`).

## Time

- **D-7** All stored and exchanged timestamps are UTC, ISO 8601, second
  precision, suffix `Z`. Local time appears only in text the harness writes
  for the user, using the configured zone.
- **D-8** Code MUST take the time from the `Clock` port. Calling
  `datetime.now()` or `time.time()` outside `ports/clock.py` is a lint
  failure (ruff rule `DTZ` plus a layout test).

## Layering

```
surface  ->  app  ->  attention | memory | writes  ->  domain, schema, ports, store
connectors  ->  ports, domain, schema.errors
```

- **D-9** `domain`, `schema`, `attention`, `memory` and `writes` MUST NOT
  import `chamberlain.connectors`.
- **D-10** `connectors` MUST NOT import `sqlite3`, `chamberlain.store`,
  `chamberlain.writes`, `chamberlain.memory`, `chamberlain.attention` or
  `chamberlain.app`. A connector does what it is told and reports what
  happened.
- **D-11** `domain` and `schema` MUST NOT import any other chamberlain
  package except each other and `limits`.
- **D-12** No module may run code at import that reads config, opens a
  database or touches the network. State arrives through `app.Context`.
- **D-13** Imports are at module top. A function-local import is allowed
  only for an optional third-party dependency.

`tests/test_layout.py` enforces D-9 to D-11.

## Canonical form and hashing

- **D-14** The canonical form of a model is its JSON with keys sorted, no
  insignificant whitespace, UTF-8, `None` fields omitted
  (`domain.canonical.canonical_json`). `content_hash` is the lowercase hex
  SHA-256 of that form. Grants, operations and the audit log all use it.
- **D-15** Email addresses are compared case-insensitively and stored
  lower-cased. A recipient MUST be a bare address (`a@b.c`), never a display
  name form.
