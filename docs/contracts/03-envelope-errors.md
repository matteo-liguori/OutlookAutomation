# 03 Reply envelope and errors

Binding for every track. Code: `src/chamberlain/schema/envelope.py`,
`src/chamberlain/schema/errors.py`.

## Envelope

Every tool reply, on every surface, is one JSON object:

```
{ ok, data, coverage, next[], guidance[], notices[], error }
```

| Field | Type | Meaning |
| --- | --- | --- |
| `ok` | bool | The tool did what was asked. |
| `data` | the tool's response model, or null | Null exactly when `ok` is false. |
| `coverage` | `Coverage` or null | Present on every list or search reply. |
| `next` | up to 3 `NextStep` | Exact tool calls the agent should make now. |
| `guidance` | `GuidanceItem[]` | Judgment text selected for this reply (07). |
| `notices` | up to 3 `Notice` | Urgent events piggybacked on this reply (07). |
| `error` | `ErrorInfo` or null | Present exactly when `ok` is false. |

- **E-1** `ok == (error is null)` and `ok == false` implies `data is null`.
  The model rejects anything else.
- **E-2** A serialised reply MUST NOT exceed `MAX_REPLY_CHARS` (6,000). The
  app enforces it last; a handler that would exceed it has a paging bug and
  the reply becomes `internal / reply.too_large`.
- **E-3** `Coverage` is `{complete, returned, total?, cursor?, gaps[]}`. A
  reply that lists or searches MUST set it. `complete: false` MUST come with
  a `cursor` (more pages) or at least one gap `{source, reason}` (something
  could not be looked at). An incomplete result MUST NOT be presented as
  complete, and an empty incomplete result MUST NOT be reported as "nothing
  found".
- **E-4** `NextStep` is `{tool, args, why}`. `args` MUST validate against
  that tool's request model. `why` is at most 120 characters.
- **E-5** `GuidanceItem` is `{id, version, text?, more?}`: either the text,
  or a pointer `more: "guide <id>"` when the budget was spent.
- **E-6** `Notice` is `{event, kind, summary, next?}`; `summary` is at most
  160 characters and written by the harness.

## Content marking

- **E-7** Text written by anyone other than the harness or the user's own
  config (mail headers and bodies, calendar text, attachment text, contact
  fields, file names from mail) MUST appear only inside a `Content` object,
  under a key named `content`. `Content` is `{origin, kind, text, truncated}`
  where `origin` is a handle or source ID.
- **E-8** `summary`, `why`, `fix`, guidance text and notices are written by
  the harness and MUST NOT interpolate remote text. They may name handles.
- **E-9** The harness never interprets `content` text as a command, a
  handle, a path or a grant.

A schema test walks every response model and fails if a free-text field that
can carry remote text sits outside `Content`.

## Errors

`ErrorInfo` is `{code, category, retryable, may_have_happened, message, fix,
field?, example?}`.

| Category | Meaning | Agent should |
| --- | --- | --- |
| `invalid_request` | The request is malformed or names something that does not exist. | Fix the call using `fix` and `example`. |
| `not_configured` | A source or setting is missing. | Tell the user what to set up. |
| `unsupported` | No enabled source has this capability, or the tool is not built. | Stop. Never substitute another route. |
| `not_authorised` | No grant covers this, or a limit was reached. | Follow `next` (usually ask the user). |
| `conflict` | State changed underneath (version, hash, already spent). | Re-read, then decide. |
| `unavailable` | The source could not be reached and nothing was changed. | Retry later if `retryable`. |
| `uncertain` | A write may have happened. | Never repeat it. Call `reconcile`. |
| `internal` | A harness bug. | Report it. |

- **E-10** `code` is `<area>.<reason>`, lower-case, from the closed
  enumeration `ErrorCode`. Adding a code is an additive contract change. The
  category, default `retryable` and default `may_have_happened` of each code
  are fixed in `ERROR_TABLE`.
- **E-11** `may_have_happened` MUST be true whenever a mutation was
  dispatched and its outcome is not proven. It MUST be false for every error
  raised before dispatch. `uncertain` errors always carry true; no other
  category may.
- **E-12** `retryable` MUST be false when `may_have_happened` is true.
- **E-13** `fix` is one sentence, at most 240 characters, saying what to do.
  For `invalid_request` on a field, `field` names it and `example` is a
  valid request for the same tool generated from the schema. Guidance never
  carries syntax; errors carry their own.
- **E-14** Code raises `ChamberlainError(code, ...)`. Raising `ValueError`,
  `RuntimeError` or a bare `Exception` for a condition the agent can meet is
  a defect. Anything else that escapes a handler becomes
  `internal / internal.unexpected`, with details in the log and not in the
  reply.
- **E-15** Error `message` and `fix` are harness text (E-8). A connector's
  raw error string goes to the log, never to the reply.

### Initial codes

| Code | Category |
| --- | --- |
| `request.invalid`, `request.unknown_tool`, `request.unknown_handle`, `request.wrong_kind`, `request.bad_cursor`, `request.too_large` | `invalid_request` |
| `card.over_cap`, `evidence.over_cap`, `note.over_cap`, `artifact.no_owner`, `artifact.owner_ended`, `artifact.hash_mismatch`, `storage.over_budget` | `invalid_request` |
| `source.not_configured`, `session.required`, `session.stale`, `config.invalid` | `not_configured` |
| `capability.unsupported`, `tool.not_implemented` | `unsupported` |
| `grant.required`, `grant.human_confirmation_required`, `grant.not_active`, `grant.scope_mismatch`, `grant.content_mismatch`, `grant.limit_reached`, `recipient.unknown` | `not_authorised` |
| `card.version_conflict`, `operation.already_settled`, `operation.intent_changed`, `remote.changed`, `remote.ambiguous`, `artifact.expired`, `cursor.expired` | `conflict` |
| `source.unavailable`, `source.sign_in_needed`, `source.busy`, `source.stale` | `unavailable` |
| `operation.uncertain`, `operation.needs_reconcile` | `uncertain` |
| `internal.unexpected`, `reply.too_large`, `store.migration_failed` | `internal` |
