# 04 Write operations

Binding for the writes track and every connector. Code:
`src/chamberlain/domain/intents.py`, `src/chamberlain/domain/operations.py`,
`src/chamberlain/writes/`.

Rule zero: **no write without verifiable authority, and no blind resend.** A resend is not
blind when the harness has looked for the first send and found it missing (W-37, W-38).

## Intents

A `WriteIntent` is the canonical, complete description of one write. Kinds:

| Kind | Fields (beyond `kind`, `source`) |
| --- | --- |
| `mail.send` | `to[]`, `cc[]`, `bcc[]`, `subject`, `body`, `body_format`, `attachments[] {artifact, sha256, filename}`, `reply_to?` (msg handle), `high_importance`, `message_id`* |
| `mail.draft_save` | as `mail.send`; `to` may be empty |
| `mail.draft_update` | as `mail.draft_save` plus `draft` (msg handle), `expected_sha256` |
| `mail.draft_discard` | `draft`, `expected_sha256` |
| `mail.move` | `message`, `to_folder`, `mark_read`, `internet_message_id`*, `from_folder`* |
| `mail.trash` | `message`, `internet_message_id`*, `from_folder`* |
| `mail.mark_read` | `messages[]` (1 to 200), `read` |
| `calendar.create` | `event` (`EventFields`, with `recurrence` for a series), `uid`* |
| `calendar.update` | `event_ref`, `expected_etag`, `event`, `scope?` (`occurrence` or `series`; 08-ports P-25) |
| `calendar.delete` | `event_ref`, `expected_etag`, `scope?` |
| `calendar.respond` | `event_ref`, `expected_etag`, `response` (`accepted`, `tentative`, `declined`), `organiser`* |

Fields marked * are left empty by the agent and completed by the harness
at `propose`, before hashing (`intents.HARNESS_COMPLETED_FIELDS`). A value
the agent supplies for one of them is ignored and replaced.

- **W-1** An intent MUST be complete at `propose` time: the harness fills
  the starred fields from the ref (`internet_message_id`, `from_folder`) and
  only then hashes and stores it. `do` takes only an operation handle and
  can change nothing.
- **W-2** `intent_sha256` is the content hash (D-14) of the completed
  intent, EXCLUDING the tokens the harness assigns (`message_id`, `uid`;
  board R1), so the same content always hashes the same. When `do` reserves the
  grant the harness derives the token from the idempotency key (W-15): the RFC
  `Message-ID` for `mail.send` and the draft kinds, the `uid` for
  `calendar.create`. That value is the operation's `reconcile_token`, so a
  later read can recognise the write. A connector proves it honours a supplied
  Message-ID with the capability limit `message_id_honoured`; where it does not,
  reconcile matches the envelope, a normalised body hash and attachment hashes.
- **W-3** Attachments come only from the artifact store and are matched by
  hash at `prepare` and again at `dispatch` (`artifact.hash_mismatch`).
- **W-4** `mail.trash` moves to the source's configured recoverable trash
  folder. There is no permanent delete intent.
- **W-5** Subjects MUST NOT contain CR or LF. Recipients MUST be bare
  addresses (D-15). `mail.send` MUST have at least one `to`.

## States

```
proposed -> authorised -> prepared -> dispatched -> confirmed
    |            |            |             |-----> failed_clean
    |            |            |             '-----> uncertain -> reconciling -> confirmed
    |            |            '-> failed_clean                        |-> confirmed_absent
    '-> cancelled '-> cancelled                                       '-> uncertain
```

| State | Meaning | Terminal | Grant use |
| --- | --- | --- | --- |
| `proposed` | Intent stored; no covering active grant yet. | no | none |
| `authorised` | `authorise()` passed; a grant use is reserved. | no | reserved |
| `prepared` | Connector's read-only `prepare` succeeded. | no | reserved |
| `dispatched` | Journalled immediately before the one mutation call. | no | reserved |
| `confirmed` | The write is proven to have happened. | yes | spent |
| `failed_clean` | Proven that nothing happened. | yes | released |
| `uncertain` | Dispatch may or may not have applied. | no | reserved |
| `reconciling` | A read-only reconcile is running. | no | reserved |
| `confirmed_absent` | Reconcile proved the write did not happen. | yes | released |
| `cancelled` | Withdrawn before dispatch, or the proposal lapsed. | yes | released |
| `abandoned` | An uncertain operation nothing can resend, closed unsettled (W-13a). | yes | kept |
| `superseded` | An uncertain send that the agent sent again after it was found missing (W-38). | yes | passed to the resend |

`proposed` and `cancelled` extend the plan's diagram: the plan starts at
`authorised`, and something has to hold the intent while the grant is being
obtained.

- **W-6** The legal transitions are exactly the arrows above
  (`domain.operations.TRANSITIONS`), plus `uncertain -> confirmed |
  confirmed_absent` taken only by the user's resolution in the viewer
  (W-13), `uncertain -> abandoned` taken only by the tick (W-13a) and
  `uncertain -> superseded` taken only when a resend is authorised (W-38). Any other
  transition is a defect and raises before touching the store.
- **W-7** Every transition is one SQLite transaction that updates the
  operation row and appends the audit row (G-20). The transition to
  `dispatched` MUST commit before the connector's `dispatch` is called.
- **W-8** **No path re-dispatches.** `dispatch` is called at most once per
  operation, ever. `uncertain` is left only through `reconciling`, which
  calls only the connector's read-only `reconcile`. A resend (W-38) is a NEW operation with
  its own one dispatch.
- **W-9** A crash is treated as the worst case. On start-up, and on every
  tick (even a throttled one, before detection), any operation found in
  `dispatched` or `reconciling` whose dispatch lease has expired moves to
  `uncertain` (R5: `lease_owner` is pid plus a process nonce, `lease_expires_at`
  is five minutes ahead; recovery touches only expired leases, and a late
  `applied` result after takeover is reconcile evidence that moves `uncertain`
  to `confirmed` through the reconcile path). Operations in `authorised` or `prepared`
  stay; `do` may be called again for them.
- **W-10** Dispatch outcomes map as follows. `applied` with a receipt:
  `confirmed`. `not_applied` (the connector proves nothing was mutated, for
  example the client was busy before the command was issued): `failed_clean`.
  `unknown`, a timeout, a lost response, or any exception escaping
  `dispatch`: `uncertain`.
- **W-11** Failures in `authorise` or `prepare` cannot have changed anything
  and MUST release the grant: the operation goes to `failed_clean` (or stays
  `proposed` when only the grant is missing). They never burn a one-off
  grant.
- **W-12** `confirmed_absent` requires the connector to report `absent` with
  `proof: true`, which a connector may do only where its capability declares
  `absence_provable` for that intent kind (P-12), and only after
  `SETTLE_AFTER` (10 minutes, never less than the connector deadline plus a 120
  second margin, because a timed-out write can land late) has passed since dispatch. Otherwise the
  operation returns to `uncertain`.
- **W-13** While an operation is `uncertain`, the grant use stays reserved,
  a one-off grant cannot be used for anything else, and the event
  `operation.uncertain` stays open. The only exit other than reconcile is the
  user resolving it in the viewer (`human:viewer` marks it `confirmed` or
  `confirmed_absent`, audited).
- **W-13a** (owner ruling: the owner is not asked what the harness or agent can settle.)
  An operation imported from an old journal keeps `summary_json` (title, time, calendar or
  recipients; never a body) and `attempted_at`, beside a NULL intent: display and matching only,
  never dispatchable. `reconcile` reads the live source and settles `confirmed` on a match
  (calendar create or update, mail send; never deletes or moves). The agent may settle
  `confirmed` with `happened` plus its own session evidence, never "did not happen": only a
  connector's proof of absence or the viewer click frees a grant. An `uncertain` operation with
  no possible resend (no intent, no grant, or a grant spent, expired or revoked) is closed
  `abandoned` by the tick after 14 days unsettled, except a mail send whose text the harness
  holds (W-40); the state is terminal, frees nothing and
  claims nothing. Attention tells the agent about such an operation once, and again only when
  `redo_at` (a later proposal of the same write) is newer than that notice.
- **W-14** An `applied` receipt carries a `Verification`:
  `client_verified` (the local client reports the result) and
  `server_verified` (read back from the server). The agent-facing text for a
  send that is only client-verified MUST say "submitted", never "delivered".
  After a confirmed send the engine SHOULD run the read-only `reconcile` to
  upgrade verification; failure to upgrade never changes the state.

## Idempotency

- **W-15** `idempotency_key = sha256(grant_handle + ":" + intent_sha256)`,
  unique in `operations`. For a `proposed` operation with no grant yet the
  key is `sha256("proposed:" + operation_handle)` and is replaced on
  authorisation.
- **W-16** `do` on an operation in a terminal state returns the stored
  receipt with the same `ok` as the first time, and does nothing else.
- **W-17** `do` on an operation in `dispatched`, `uncertain` or
  `reconciling` fails with `operation.needs_reconcile`
  (`may_have_happened: true`) and a `next` of `reconcile`.
- **W-18** `propose` with an intent whose hash matches a live operation
  under the same grant returns that operation. A changed intent is a new
  operation and needs its own authority; it can never inherit a one-off
  grant (`grant.content_mismatch`).
- **W-19** A standing grant may authorise the same content twice only if
  its scope sets `allow_repeat`. Otherwise an identical intent within 24
  hours returns the earlier operation.

## Authorisation

```python
def authorise(intent, operation, grants, usage, policy, now) -> Decision
```

- **W-20** `authorise` is a pure function in `writes/authorise.py`: no
  clock, no store, no connector. `Decision` is `allow(grant)` or
  `deny(code, requirement)`.
- **W-21** `authorise` runs at `propose` (to say what is needed) and again
  inside `do`, in the same transaction that reserves the grant use. A grant
  that expired, was revoked or reached a limit between the two calls denies.
- **W-22** Preference order: an active one-off grant whose `content_sha256`
  equals `intent_sha256`; then the narrowest active standing grant whose
  scope covers the intent. Scope matching is exact-field, never fuzzy.

## Read state and partial results

- **W-23** Changing read state is a write (`mail.mark_read`, or
  `mail.move` with `mark_read: true`) and needs a grant whose scope allows
  it. No read path may change it (P-3).
- **W-24** `mail.mark_read` over several messages reports per-message
  results. If some were marked and some failed, the operation is
  `confirmed` with a receipt listing both; it is not retried as a whole.

## Compaction

- **W-25** 30 days after reaching a terminal state an operation row is
  compacted: `intent_json`, `prepared_json` and the receipt body are
  replaced by a one-line receipt (kind, source, state, hashes, times). The
  compact row is deleted with its audit window (M-3). Operations that are
  not terminal are never compacted or deleted.

## Connector duties (summary of 08)

`prepare(intent, refs)` is read-only and resolves everything `dispatch`
needs; `refs` maps each handle in the intent to its stable locator, which
the engine looks up because a connector never sees the store. The engine
stores `refs` and `Prepared` on the operation.
`dispatch(prepared, key)` performs the one mutation and returns an outcome.
`reconcile(prepared, key)` is read-only and returns `present`, `absent` or
`unknown`. The engine owns ordering, journalling and grants; a connector
keeps no journal of its own.

## Property tests that define done

Hypothesis sequences of propose, confirm-grant, do, timeout, crash, restart
and reconcile over the fake provider with random faults, checking:

1. no grant ever yields two `confirmed` operations beyond its limit, and a
   one-off never yields more than one remote effect;
2. the fake's dispatch log never shows two dispatches for one operation;
3. every terminal operation has a receipt, and every transition has an
   audit row whose hash chain verifies.

## Review rulings as built (board, 2026-11-24)

- **W-26** (R6) Immediate settle. After a timeout or an unverified result the
  engine runs bounded read-only reconciles before declaring `uncertain` and
  raising the event: mail up to 3 attempts within 30 seconds, calendar up to
  7 attempts within 60 seconds.
- **W-27** (R3) `propose` for `mail.send` runs the connector's read-only
  `prepare` before the operation is stored, so the duplicate scan over Sent,
  Drafts and Outbox (normalised subject, body match, recipient overlap) fails
  early with a conflict error and leaves nothing behind (T-13). An incomplete
  scan or a match fails unless the covering grant's scope sets
  `allow_duplicate`; the engine tells `prepare` through the reserved ref key
  `_allow_duplicate` = "1". `do` runs `prepare` again.
- **W-28** (R2) `ReconcileStatus.QUEUED`: an exact copy sits in Outbox or
  Drafts awaiting submission. It carries no receipt and no proof, keeps the
  operation `uncertain` and never releases the grant. Any `absence_provable`
  claim for sends must cover Sent, Outbox and Drafts.
- **W-29** (R4) Move, trash and draft-update receipts return the NEW locator;
  on `confirmed` the engine calls `RefResolver.repoint(old, new)` so a handle
  survives filing.
- **W-30** (R7) `ProviderReceipt.warnings` carries harness-safe codes
  (`merged_existing_copy` when a Gmail trash merges into an existing copy) and
  `read_state_preserved` says whether the flag survived. The operation is
  `applied`, not failed; both surface on `OperationView.warnings`.
- **W-31** (R11) If the Sent copy is missing but the exact message (token or
  content match) arrived in the sender's own Inbox for a self-addressed send,
  reconcile counts it `present`.
- **W-33** (owner ruling 2026-11-24) Invitations. A `calendar.create` or `calendar.update` whose
  `event.attendees` is not empty is an invitation: the calendar service sends it, changes it and
  cancels it (`calendar.delete` on the organiser's copy); the harness sends no mail itself.
  `calendar.respond` answers an invitation the owner received by changing only the owner's own
  answer on the owner's copy. It names the organiser, which the harness fills at `propose` from
  the stored event (W-1: an agent value is replaced), and fails with `request.invalid` when the
  event is not an invitation to this calendar's owner.
- **W-34** A source declares the capability `calendar.invite` only if it can send, change and
  cancel invitations and answer them. A write that reaches other people (attendees on a create or
  update, stored attendees on an update or delete, any respond) on a source without it fails at
  `propose` with `capability.unsupported` and detail `invitations-not-supported`, before anyone is
  asked to confirm. The Owl calendar through Thunderbird does not declare it: the add-on's calendar
  API documents no way to send or answer an invitation, and the harness does not guess at Owl's own
  handling.
- **W-35** Only the organiser changes or cancels an invitation. `update` and `delete` on the
  owner's copy of someone else's invitation fail at `propose` with `capability.unsupported` and
  detail `invitee-copy`; the owner's write there is `calendar.respond`.
- **W-36** Verified outcome. A connector reports `server_verified` for an invitation only when the
  read-back event on the organiser's own calendar carries the attendee set that was written
  (addresses, case-insensitive; the organiser's own address is ignored). Delivery to an invitee is
  never claimed: the harness cannot see it from the organiser's side. The organiser's later view of
  each reply is `CalendarEvent.participants`.
- **W-42** Repeating events (08-ports P-23 to P-27). `propose` reads the event a handle names and
  checks `scope` against it (single event: none; one occurrence: `occurrence`; the series:
  `series`), the capability (`calendar.occurrence`, `calendar.recurrence`) and that no guests are
  involved, before anyone is asked. It keeps the date or the rule in the stored refs
  (`_event_when`, `_event_repeats`) so the preview and the viewer say plainly whether a change
  reaches "this date only" or "every date" and how often the event repeats. A series' `zone` is
  completed at `propose` (W-1) from the owner's zone. Permissions are those of the same write on
  a single event; `describe_intent` of an event that does not repeat is unchanged.
- **W-32** Calendar writes on events without attendees need
  only `agent_evidence` (G-12a). For `update` and `delete`, `propose` reads the
  stored event and keeps the attendee fact in the stored refs
  (`_event_attendees`).

## An uncertain send is never lost (owner ruling 2026-11-24, W-37 to W-41)

The first 24 hours keep the strict rule: an uncertain send is never sent again, in any form.
After that the harness looks once, and the agent decides.

- **W-37** The one look. The tick looks again at every `mail.send` that has been `uncertain` for
  `UNCERTAIN_SEND_RECHECK_HOURS` (24, counted from `dispatched_at`). It reads through the same
  `reconcile` path as always: the connector's Sent, Outbox and Drafts of the sending mailbox, then,
  for each recipient that is an owner address of another configured source, that source's Inbox
  (a read-only search; a copy counts as found when its Message-ID matches, or when subject, sender,
  time and text all match). Never `confirmed_absent`: the tick has no authority to free a grant.
  - Found in Sent or a recipient's Inbox: settles `confirmed`, as `reconcile` would.
  - Still waiting in the Outbox or Drafts: stored as `queued`.
  - Found nowhere: stored as `missing`, with where it looked (`recheck_json`, place names only).
  - The connector could not say (`unknown`): nothing is stored and the next tick looks again.
  The result is on the operation (`rechecked_at`, `recheck_result`, `recheck_json`), shown on
  `OperationView.recheck`, and audited as `operation.rechecked`. A later `reconcile` by the agent
  of a re-checked send updates `queued` and `missing` in the same way.
- **W-38** The resend. `propose` with `redo_of: <operation>` makes a NEW operation from the
  original's stored text (the agent supplies no content), linked by `redo_of`. It is refused
  unless the original is `uncertain`, a mail send with stored text, past its re-check, itself not a
  resend, and a fresh read-only look still says `missing` (found: it is settled `confirmed` and
  the resend is refused; `queued`: refused, the message is stuck, not lost; unreadable: refused).
  It is authorised only by the ORIGINAL's grant, with no new evidence: the grant must still
  authorise it (a one-off that merely timed out while unspent still does; a revoked, declined or
  suspended one, an exhausted limit or a changed scope does not, and the error tells the agent to
  ask the owner). The same checks run again at `do`. The resend takes the duplicate scan of W-27.
  - `do` on the resend, in the transaction that reserves it, moves the original to `superseded`.
    `superseded` is terminal, releases the grant use and passes it to the resend, so the grant is
    still used once. The original stays `uncertain` and visible until then (a proposal that is
    never run changes nothing).
  - One resend per original: a unique index on `redo_of` (cancelled proposals aside) and an
    existing resend is returned by a repeat `propose`. A second `propose` of the same text returns
    the resend too (W-18). A resend is never itself resent; a second one needs the owner's word.
  - Both operations are audited (`operation.proposed` with the link, `operation.superseded`).
  - If the resend then fails clean (for example the client is down), the original is already
    `superseded` and nothing was sent; the agent is told so, and proposes the write again, which a
    still-active grant allows.
- **W-39** Telling the agent. A re-checked send leaves `operation.uncertain` and raises ONE
  `send.missing` event (priority 94, never closes on its own, only when the send is settled or
  superseded), `reason` `missing` or `outbox`. Its text names recipient, subject and age; the card is
  an attribute; `open` on the operation shows `recheck`, `redo_of` and `redone_by`. The rules
  `send.missing` and `send.stuck` carry the judgement: weigh urgency against the small cost of a
  duplicate, resend or ask the owner, and say which in the report. `send.uncertain` keeps the strict
  wording for the first 24 hours.
- **W-40** No quiet closing of a send. `abandon_stale` and attention's "told once, then quiet"
  rule skip a `mail.send` whose text the harness holds. It stays visible until the agent resends it,
  the agent settles it `happened` with evidence, or the owner resolves it in the viewer (W-13).
  Calendar writes, other mail writes and sends imported from an old journal (no text, never
  resendable) keep the 14-day close of W-13a.
- **W-41** The owner's viewer lists each uncertain operation with the re-check result, when it
  looked and where, and any resend.

- **W-43** Marking mail read after a report. When a run finishes with a kept report and
  `read_state.reporting_marks_read` is on, the harness marks read each message of that run's session
  that was shown to the agent in a mail batch, was triaged, was not sent mail and has not been tried
  (`mail_seen.read_marked_at`). Mail that arrived after the batch was shown is never in it. The write
  is an ordinary `mail.mark_read` operation with per-message results (W-24), authorised by the
  mailbox's standing grant (05, G-24), actor `system:read_state`. A message is tried once.
