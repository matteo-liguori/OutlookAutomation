# 05 Grants, confirmation and audit

Binding for the writes track; read by attention (events) and the viewer.
Code: `src/chamberlain/domain/grants.py`, `src/chamberlain/writes/`.

## Kinds

| Kind | Binds | Uses |
| --- | --- | --- |
| `one_off` | One exact intent, by `content_sha256 == intent_sha256`. | 1 |
| `standing` | A `StandingScope` and its limits. | Many, within limits |

`StandingScope`: `source`, `intent_kinds[]`, `recipients[]` (exact
addresses) or `recipient_domains[]`, `folders[]`, `calendar?`,
`allow_mark_read`, `allow_repeat`, `card?` (bind to one card), `max_per_day`,
`title` (80), `summary` (300).

- **G-1** A standing scope MUST name a source and at least one intent kind.
  A standing scope for `mail.send` MUST name recipients or domains and a
  `max_per_day`. There is no wildcard scope.

## Lifecycle

```
proposed -> active -> spent        (one_off, its operation confirmed)
    |          |----> revoked      (user, or agent via forget)
    |          |----> expired      (expires_at passed)
    |          '----> suspended -> active   (review overdue; user keeps it)
    |-> declined   (user said no)
    '-> lapsed     (proposal not answered in 7 days)
```

- **G-2** **Grants are never deleted by use, revocation or expiry.** These
  are status changes with a timestamp and an audit row. A terminal grant is
  compacted after 90 days (scope and content replaced by title, kind, hashes
  and times) and the compact row leaves only with its audit window (M-3).
- **G-3** A grant's `kind`, `scope`, `content_sha256`, `granted_at`,
  `expires_at` and `review_at` are immutable once `active`. Changing any of
  them means a new grant, proposed and confirmed afresh, with
  `supersedes` pointing at the old one, which becomes `revoked`. No tool and
  no agent call can edit them.
- **G-4** `spent` and every other terminal state are final. A spent one-off
  cannot be reactivated.
- **G-5** A one-off grant expires 7 days after activation unless the
  confirmation set an earlier time. A standing grant expires at most 365
  days after `granted_at`.
- **G-6** Standing grants are reviewed monthly on the anniversary of
  `granted_at`, in the configured zone, clamped for short months and stable
  across daylight-saving changes; the schedule never drifts from the grant
  date. An imported grant with no known grant date is due for review at
  once.
- **G-7** Reading, listing or using a grant never renews it or moves its
  review date. Only the user's "keep" in the viewer advances `review_at` to
  the next anniversary, and it changes nothing else.
- **G-8** When `review_at` passes, attention raises `grant.review_due`.
  14 days later an unreviewed standing grant becomes `suspended` and
  authorises nothing until kept. An expired grant still shows as needing
  review; it is never silently reported as available.
- **G-9** A grant bound to a card (`card` set) is revoked when that card
  closes. A grant with no card survives any card's closure.
- **G-10** A grant in use is protected: while any operation that reserved
  it is non-terminal, the grant row and its content are never compacted, and
  expiry of the grant does not release the operation (it stays `uncertain`
  until reconciled).

## What frees a grant

| Operation outcome | One-off grant | Standing grant counter |
| --- | --- | --- |
| `confirmed` | `spent` | use counted |
| `failed_clean`, `confirmed_absent`, `cancelled` | stays `active` | use released |
| `uncertain`, `reconciling`, `dispatched` | held; unusable | use counted |

- **G-11** Usage is derived from `operations`, not stored as a counter that
  can drift: reserved or spent uses are operations under the grant in any
  state except `failed_clean`, `confirmed_absent`, `cancelled`.

## Who may activate a grant

| Situation | Requirement |
| --- | --- |
| Any standing grant | `human_confirmation` (mandatory) |
| `mail.send` with any recipient not in `known_recipients` for that source | `human_confirmation` (mandatory) |
| `calendar.create`, `update` or `delete` on an event with attendees | `human_confirmation` (mandatory) |
| `mail.send` to known recipients only | config `grants.send_known`, default `agent_evidence` (owner decision 2026-11-24) |
| `mail.draft_*`, `mail.move`, `mail.trash`, `mail.mark_read`, `calendar.create`, `update` or `delete` on an event without attendees | config `grants.other`, default `agent_evidence` |

- **G-12a** (owner ruling 2026-11-24) The human is required only for standing
  grants, sends to unknown recipients and calendar writes on events with
  attendees. For `calendar.update` and `calendar.delete` the event is the stored
  one: `propose` reads it and records the fact in the operation's `refs`
  (`_event_attendees`); an update that lists attendees counts too. The etag in
  the intent makes the fact binding: an event that gained attendees since the
  read no longer matches. A standing grant never covers an update or delete.
- **G-12** Config may tighten any row to `human_confirmation`. It MUST NOT
  loosen a mandatory row; a config that tries is `config.invalid`.
- **G-13** `agent_evidence`: the agent calls `propose` with the operation
  and an `evd_` handle recording the user's words. The grant becomes
  `active` with `confirmed_via: agent_evidence` and that evidence gains the
  grant as an owner. The evidence MUST have been created in the same session
  and MUST NOT be `content` from a remote source.
- **G-13a** (owner decision 2026-11-24) Evidence with an `instruction:` ref
  (an authenticated email, T-10a) also satisfies G-13 for an `agent_evidence`
  action: from the owner's own address for any kind; from another listed
  sender only for the intent kinds in that sender's `scope` (`ask_only`
  covers none). It never satisfies `human_confirmation`, and it must be at
  most 7 days old when `keep` accepts it.
- **G-14** `human_confirmation`: only the confirmation channel below can
  activate. `propose` with `approval_evidence` on such a grant fails with
  `grant.human_confirmation_required`; its `fix` tells the agent to ask the
  user to open the viewer. The viewer URL in that text carries no token.
- **G-15** (amended by owner decision 2026-11-24) A recipient is known for a
  source when (a) a human-confirmed grant naming it was activated
  (`origin: confirmed-grant`), (b) it appears as To or Cc on a message in
  that source's Sent folder, read through the connector by
  `chamberlain recipients seed` or the importer (`origin: sent-history`), or
  (c) it is the source's own mailbox address, which counts without a row.
  Receiving mail from an address, or the agent sending to it under
  `agent_evidence`, does not make it known. Seeding is read-only and
  never touches grants.

## Send limits

- **G-16** The engine refuses `mail.send` beyond `SENDS_PER_HOUR` (20) per
  source, counted from operations dispatched in the last hour, and beyond a
  standing grant's `max_per_day`. Code `grant.limit_reached`. Limits are
  checked in `authorise`, inside the reserving transaction.

## Confirmation channel (the viewer endpoint)

The writes track builds this endpoint and its tests. The viewer UI is wave 2.

- **G-17** Served by `chamberlain viewer` on `127.0.0.1` only, on a random
  port, never on a routable address. Every request MUST carry the viewer
  token in the `X-Chamberlain-Token` header and, for state-changing
  requests, an `Origin` equal to the viewer's own origin. Requests failing
  either check get 403 and an audit row.
- **G-18** The token is created at viewer start, kept in memory and in a
  user-only file under the data root's `run/` folder, and MUST NOT be
  returned by any tool, written to any reply, log line or artifact, or
  accepted as a tool argument. No tool or CLI command activates, keeps or
  edits a grant. This raises the bar against a manipulated agent; it is not
  a sandbox, and the contract says so in `doctor`.
- **G-19** Endpoints (JSON):

  | Method and path | Effect |
  | --- | --- |
  | `GET /api/grants?state=` | List grants (handle, kind, state, title, times). |
  | `GET /api/grants/{handle}` | Exactly what would be authorised: for a one-off, the full rendered intent (recipients, subject, body, attachment names and hashes); for standing, the scope and limits; plus `content_sha256`. |
  | `POST /api/grants/{handle}/confirm` | Body `{content_sha256, expires_at?}`. Activates only if the hash equals the stored one (what the person saw is what is granted); otherwise 409. |
  | `POST /api/grants/{handle}/decline` | `proposed -> declined`. |
  | `POST /api/grants/{handle}/revoke` | `active|suspended -> revoked`. |
  | `POST /api/grants/{handle}/keep` | Review: advances `review_at` (G-7). |
  | `POST /api/operations/{handle}/resolve` | Body `{outcome: happened|did_not_happen}` for an `uncertain` operation (W-13). |

  Every state change writes an audit row with actor `human:viewer`.

## Audit log

- **G-20** `audit_log` is append-only. Each row: `seq`, `at`, `actor`
  (`agent:<session>`, `human:viewer`, `system:tick`, `system:import`),
  `kind` (`tool.call`, `grant.*`, `operation.*`, `sweep`, `import`,
  `confirm.rejected`), `subject` (handle), `summary` (harness text, 240),
  `payload_sha256`, `prev_hash`, `hash`, where `hash = sha256(prev_hash +
  canonical row)`. No code path updates or deletes a row inside the
  retention window.
- **G-21** The audit log stores hashes and handles, never mail bodies or
  recipient lists in clear. `doctor --check audit` verifies the chain and
  reports the first broken `seq`.
- **G-22** Rows older than the audit window (730 days) are removed by the
  sweeper in whole months, leaving a checkpoint row carrying the last
  removed hash so the remaining chain still verifies.
- **G-23** Every tool call writes one `tool.call` row (tool, session,
  request hash, `ok`, error code). Every grant and operation transition
  writes one row in the same transaction as the change.
