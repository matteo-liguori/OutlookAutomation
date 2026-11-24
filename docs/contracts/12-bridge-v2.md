# 12 Thunderbird bridge protocol, version 2

Binding for the Thunderbird connector (`src/chamberlain/connectors/thunderbird/`)
and its add-on (`.../addon/`). Written by wave-1 track (d). The machine-readable
form is `protocol.py`; the add-on's TypeScript types and runtime request
schemas are generated from it (`typegen.py`, section 9). Where this prose and
`protocol.py` disagree, post on the board.

The bridge has three parts and a strict division of duty:

```
 Python client  --loopback TCP-->  native host  --native messaging (stdio)-->  add-on
 (MailPort)                         (relay, auth)                              (messenger.*)
```

- The **client** (`client.py`, `mail.py`, `contacts.py`) decides what a result
  means. It maps add-on facts to `Page`, `DispatchResult` and `ReconcileResult`.
- The **host** (`native_host.py`) authenticates clients, checks the handshake
  and relays frames. It never interprets a command beyond the closed list.
- The **add-on** executes one command, verifies what it can, and reports facts.
  It never checks grants, keeps no journal, never retries and never decides
  whether absence is proven.

## 1. Rules

- **B-1** Every message carries the protocol version `v` (2) and a request ID `id`.
- **B-2** The command list is closed (section 5). The host refuses a command
  outside it and the add-on refuses it again; neither executes anything else.
- **B-3** The add-on never opens a network listener, never calls `eval`,
  `new Function` or a string `setTimeout`, never runs a shell command, and has
  no command that takes code, a path or a URL. The experiment API exposes a
  fixed set of named functions, not a generic `execute`.
- **B-4** The add-on keeps no journal and no idempotency record. It holds only
  in-memory transfer state for the request in flight (attachment chunks) and
  one write-busy flag.
- **B-5** A write command is executed at most once per request ID. The add-on
  refuses a second in-flight request with the same ID. Nothing retries.
- **B-6** Messages cross the bridge by stable identity (`folder_id` plus bare
  `internet_message_id`), never by Thunderbird's session integer (P-6). Header
  results carry `session_id` for diagnostics only.

## 2. Frames

Every frame is a 4-byte unsigned little-endian length and that many bytes of
UTF-8 JSON (one object). `frames.py` is the reference.

| Direction | Limit (bytes) | Why |
| --- | --- | --- |
| host to add-on | 900,000 | Mozilla caps native messaging at 1 MiB into the browser |
| add-on to host | 67,108,864 | |
| client to host and back | 67,108,864 | |

- **B-7** A zero length, a length over the direction's limit, a header cut
  short, a body cut short and invalid JSON are each refused. An incoming
  fault raises `source.unavailable`; an outgoing frame over the limit raises
  `request.too_large` before any byte is sent.
- **B-8** Attachment bytes never ride inside a request or response body. They
  travel as `chunk` frames (section 4).

## 3. Messages

`t` selects the message type.

| `t` | From | Purpose |
| --- | --- | --- |
| `addon_hello` | add-on | First message after connecting: `v`, `addon_version`, `browser_version`, `capabilities[]` |
| `client_hello` | client | First message of a connection: `v`, `token`, `client_version`, `require[]` |
| `welcome` | host | Authenticated and compatible: `host_version`, `addon_version`, `capabilities[]` |
| `refusal` | host or add-on | Refuses to operate: `code`, `message` |
| `request` | client (relayed) | `cmd`, `args` |
| `chunk` | either | `name`, `index`, `data` (base64, at most 196,608 raw bytes), `sha256` of the raw chunk |
| `response` | add-on (relayed) | `cmd`, `ok`, `result` or `error`, `chunks` |

### Handshake

1. The add-on connects to the host and sends `addon_hello`. The host stores it
   and replaces any earlier add-on connection.
2. A client opens a loopback connection and sends `client_hello` with the
   session token. A wrong token gets `refusal unauthenticated` and the
   connection closes; nothing else is read from it.
3. The host answers `refusal addon_not_connected` if no add-on has said hello;
   `refusal version_mismatch` if the add-on's or the client's `v` is not 2;
   `refusal capability_missing` if the add-on lacks any capability in the
   client's `require` list. Otherwise it sends `welcome`.
4. The client checks the `welcome` itself (version, required capabilities) and
   refuses to operate on any mismatch.

- **B-9** A refusal maps to `source.unavailable` with a `fix` naming the
  add-on update ("Install the current Chamberlain Thunderbird add-on and
  restart Thunderbird"). A request is never sent after a refusal, so a refused
  write is `not_applied`.
- **B-10** Capabilities are `read search attachments send draft move trash
  mark_read contacts refresh`. The mail client requires all of them. Other
  capabilities (for example `calendar`, added by the calendars track) are
  optional: an add-on may advertise them, a client names in `require` only
  what it needs, and the host checks `require` against the advertised list
  without knowing what the strings mean.

## 4. Chunked binary transfer

- **Upload** (`send`, `draft_save`, `draft_update`): the request's
  `args.attachments[]` lists each `AttachmentSpec` `{key, name, size, sha256,
  content_type, chunks}`. The request frame is followed on the same connection
  by exactly `chunks` `chunk` frames per attachment with `name` equal to the
  spec's `key`, `index` 0 to `chunks-1` in order. The add-on holds the request
  until every chunk has arrived, checks each chunk hash, the size and the
  whole-file hash, and only then executes. Any failure before execution is an
  error with `mutation: none`. A transfer not completed within 60 seconds is
  dropped.
- **Download** (`attachment`): the add-on sends `chunks` `chunk` frames with
  the request's ID and `name` equal to the part name, then the `response`
  whose `result` carries `size`, `sha256` and `chunks`. The client assembles,
  verifies each chunk hash, the byte count and the whole hash, and returns
  nothing on any mismatch (P-8). No partial file is ever created: bytes stay
  in memory until verified.
- The connector limits one attachment to `max_attachment_bytes` (25 MiB).

## 5. Commands (closed list)

All mail commands carry `account_id` and `mailbox`; the add-on verifies the
account exists and one of its identities equals `mailbox`
(`account_mismatch` otherwise) and that every folder named belongs to that
account (the local Outbox excepted, section 8). Argument and result shapes are
the models of `protocol.py`.

**Reads** (never change remote state, including the read flag; the add-on
captures the flag before and after and fails with `changed` if it moved):
`status`, `accounts`, `folders`, `query`, `read`, `attachment`, `resolve`,
`refresh`, `contacts`, `contact_read`.

- `query` filters by folder, bare Message-ID, text, subject (normalised:
  reply prefixes and case ignored), unread, window. Results are ordered newest
  first by `(date_ms, folder_id + NUL + message_id)` and paged by an explicit
  keyset cursor (`after`), so a cursor stays valid when mail arrives. When the
  scan stops at `max_scan` the result says `complete: false`.
- `read` returns `authentication_results`: every `Authentication-Results`
  header of the message, topmost first, each clipped to 2000 characters (at
  most 8). The receiving provider adds the topmost; the sender can add the
  others, so only the topmost is evidence (`domain/sender_auth.py`).
- `resolve` returns every physical copy of one Message-ID in one folder.
- `refresh` asks the provider to synchronise exactly the named folders (at
  most 8) and reports the provider callback per folder. It is a read: it
  pulls remote state and changes none. The harness sends 3 folders a call with
  `timeout_ms` 8000, so a call answers inside the add-on's 30-second read deadline even when
  every folder is stuck (08-ports P-20). An account root, a container with no mail (Gmail's
  `[Gmail]`) and a saved search answer `success: false, code: "not-selectable"` at once; the
  harness counts that as in sync. Add-on 2.0.1 added this answer; with 2.0.0 such a folder
  times out and is reported as not synced, so the add-on needs reinstalling.

**Writes**: `send`, `draft_save`, `draft_update`, `draft_discard`, `move`,
`trash`, `mark_read`. Each performs exactly one logical mutation.

- `send` composes with the bound Sent folder applied explicitly (also on
  replies), verifies the composer against the request (sender, subject, body,
  recipients, Sent folder, priority, attachment bytes), then submits. It adds
  the header `X-Chamberlain-Write-Token: <token>`. Thunderbird does not let an
  add-on choose `Message-ID` (R1): the connector declares
  `message_id_honoured: 0` and reconciles by content (section 6).
- `draft_save` / `draft_update` save into the bound draft folder with
  autosave paused in the managed composer; a manual edit by the user fails the
  composer verification (`changed`). `draft_update` first checks the existing
  draft's raw hash equals `expected_raw_sha256`, reopens it through the
  experiment API, rewrites it, saves, and reports how many copies of the old
  draft remain. `display_only` leaves the composer open.
- `draft_discard` closes a managed composer for the draft if the draft's raw
  hash still equals `expected_raw_sha256` and the composer is unmodified. It
  NEVER deletes saved mail (R8).
- `move` and `trash` locate the one copy at `ref`, refuse if the destination
  already holds one (move) or the source holds several (`ambiguous`), move it,
  then poll up to 20 times at 250 ms until exactly one more copy is at the
  destination and one fewer at the source; otherwise `not_verified`
  (`mutation: possible`). The flag is restored to its prior value unless
  `mark_read`. `trash` accepts a provider that merges the moved copy into an
  existing Trash copy (Gmail labels): the result then has
  `merged_existing_copy: true` and `read_state_preserved: false` when the flags
  differed; existing Trash flags are never altered (R7).
- `mark_read` sets the flag (`read: true` or `false`) on each located copy and
  reports every item; partial success is a normal `ok` result (W-24).

**Reconcile twins** (read-only; they may `refresh` the exact folders involved
first): `reconcile_send`, `reconcile_draft_save`, `reconcile_draft_update`,
`reconcile_draft_discard`, `reconcile_move`, `reconcile_trash`,
`reconcile_mark_read`. They take the write's arguments minus bytes, return
observations (copies found, per-folder completeness, whether the refresh was
verified) and never a verdict.

**Calendar commands** (optional capability `calendar`): `calendar_list`,
`calendar_query`, `calendar_read` (reads) and `calendar_create`,
`calendar_update`, `calendar_delete` (writes, sharing the one write slot with
mail). They take a Thunderbird `calendar_id` and no account fields. The add-on
advertises `calendar` only when its privileged calendar helper
(`experiment/calendar_api.js`, eight named functions, no generic execute) loads;
only a client that uses these commands lists `calendar` in `require`, so mail
works unchanged without it. Events cross the bridge as iCalendar text.

- `calendar_list {}` returns `{calendars: [{id, name, type, read_only, disabled,
  fresh_as_of?}]}`. `fresh_as_of` is sent only when the client can say when the
  calendar last synchronised; the Owl helper cannot today, so the connector's
  health is `stale` until a live check finds a source for it.
- `calendar_query {calendar_id, start, end, limit}` (ISO times, `limit` 1 to
  5000) returns `{items: [{id, recurrence_id|null, icalendar}], complete, gaps}`:
  the occurrences inside the window (a series appears as its occurrences, each
  with `recurrence_id` as `YYYYMMDDTHHMMSSZ` or `YYYYMMDD`), cancelled events
  included, ordered by start then id. `complete: false` with no gap means the
  scan stopped at `limit`; an offline Thunderbird adds a gap and the result is
  incomplete.
- `calendar_read {calendar_id, event_id}` returns `{item: {...} | null}`: the
  exact stored text, or null when the id is not there.
- `calendar_create {calendar_id, event_id, icalendar}` refuses with `changed`
  when the id exists. `calendar_update {..., expected_icalendar, icalendar}` and
  `calendar_delete {..., expected_icalendar}` refuse with `changed` unless the
  stored text equals `expected_icalendar` exactly (the connector's etag is its
  hash), and with `not_found` when the event is gone. Delete is a real
  `deleteItem`.
- **Refusals before any provider call carry `mutation: none`**: unknown or
  disabled calendar (`not_found`); read-only calendar (`unsupported`); offline
  (`client_offline`); text that is not one valid master event, or whose UID is
  not `event_id` (`bad_request`); and any event, stored or supplied, with an
  attendee or an organiser (`unsupported`: the harness sends no invitations).
- The add-on marks the mutation immediately before the provider call; a failure
  from then on, including a deadline, is `mutation: possible`. After the call it
  reads the event back: a create or update the provider did not keep, or a
  delete that left the event, is `not_verified` with `mutation: possible`. A
  success returns `{item, timing_ms}` where `item` is the read-back (null after
  a delete) and `timing_ms` maps each stage (`calendar`, `read_before`,
  `provider`, `read_after`) to milliseconds since the command began.
- There is no calendar reconcile twin: reconcile is `calendar_read` or a bounded
  `calendar_query`. Absence through a client cache is never proof.

## 6. What the client concludes (reconcile)

- **move/trash**: `present` when the source holds no copy and the destination
  holds one more than at `prepare` (trash: at least one); `absent` when the
  source count is unchanged and the destination count is unchanged;
  `proof: true` only if both folders were refreshed with a verified provider
  callback and both queries were complete. Anything else is `unknown`.
- **mark_read**: `present` when every copy has the wanted flag; `absent` when
  none do (proof as above); a mix is `unknown`.
- **draft_save / draft_update**: matched by content fingerprint (envelope,
  normalised body hash, attachment hashes, priority, reply header) or the token
  header, in the draft folder; absence is proven by a complete refreshed scan of
  the draft folder.
- **send**: matches are searched in Sent, Drafts, Outbox and, for a send to the
  sender's own address, the Inbox. Sent (or the own Inbox, R11) is `present`
  with `server_verified` only for the Sent read-back. An exact copy only in
  Outbox or Drafts is `queued` (R2). No match is `absent` with `proof: false`
  always: the `absence_provable` set for `mail.send` is empty, because a
  provider can accept a submission and fail to file the Sent copy (the KCL
  incident), so no folder scan proves nothing was submitted.
- Several exact matches in one folder are `unknown`, never `present`.

## 7. Errors

An error response carries `code`, `message`, `mutation` (`none` or `possible`)
and `retryable`. The add-on sets `mutation: none` only when it proves no
mutating `messenger` call was issued for the request; the flag is an in-memory
marker set immediately before that call.

| Add-on code | Meaning | Connector maps to |
| --- | --- | --- |
| `bad_request`, `unknown_command` | malformed args, command outside the list | `request.invalid`, `capability.unsupported` |
| `account_mismatch` | account or identity not as bound | `source.not_configured` |
| `not_found` | the named message or folder is not there | `request.unknown_handle` |
| `ambiguous` | more than one copy where one is required | `remote.ambiguous` |
| `changed` | a precondition failed (hash, parent, composer edited) | `remote.changed` |
| `busy` | another write is in progress (`mutation: none`) | `source.busy`; dispatch is `not_applied` |
| `timeout` | the add-on deadline passed | reads `source.unavailable`; dispatch `unknown` |
| `client_offline` | Thunderbird is offline | `source.unavailable` |
| `not_verified` | the mutation was issued but not verified | dispatch `unknown` |
| `too_large` | over a size limit | `request.too_large` |
| `hash_mismatch` | transferred bytes do not match their hash | `artifact.hash_mismatch` |
| `unsupported` | the client lacks the API | `capability.unsupported` |
| `internal` | add-on fault | `source.unavailable` |

Dispatch outcome rules (P-10):

- `applied`: a `response` with `ok: true` for the write (plus the connector's
  own checks of the result).
- `not_applied`: connecting or the handshake failed before the request was
  sent; or an error with `mutation: none`; or the connector's own hash check
  of the payloads failed before any byte was sent.
- `unknown`: everything else, including any timeout, a closed connection after
  the request began, a malformed response, and `mutation: possible`.

## 8. Scope of reads

- The local **Outbox** belongs to the local-folders account. The add-on
  reads it only through `folders` with `include_outbox` and `query` with
  `own_only` (messages authored by `mailbox`), because the queue is shared
  across accounts.
- The connector scans Sent, Drafts and Outbox (R3) before a send and again for
  send reconcile. Coverage is reported per folder; an incomplete scan fails the
  send at `prepare` with `remote.changed` unless the grant allows duplicates
  (the engine passes `refs["_allow_duplicate"] = "1"`).

## 9. Generated types

`python -m chamberlain.connectors.thunderbird.typegen` writes
`addon/src/protocol.generated.ts`: one TypeScript interface per model, the
`CommandArgs` and `CommandResults` maps, the constants, and the JSON Schema of
every request (`SCHEMA_DEFS`) which the add-on interprets at run time to
validate each request before executing it. A test regenerates and compares.

## 10. Transport and timing

- The host listens on `127.0.0.1` on a random port and writes
  `{pid, port, token, protocol}` to `runtime.json` in the bridge directory
  (`<data root>/bridge/`, R9), created for the current user only (on Windows
  `icacls /inheritance:r`). The token is 256 random bits; compare in constant
  time. A client without the token is refused and nothing it sends is relayed.
- A dropped client, a bad frame or a client that connects and leaves ends only
  its own connection (never the host). A relay that cannot be completed tells
  the client `bridge response uncertain` and never retries.
- **Deadlines nest** (incident: a host deadline shorter than the add-on's
  hid the add-on's specific error): add-on read 30 s, write 45 s; host relay
  read 38 s, write 53 s; client socket read 43 s, write 58 s. A test checks
  the ordering.
- After an add-on deadline passes the write-busy flag stays set until the
  task actually settles, so a late write cannot overlap a new one.

## 11. Locators

`tb:` + base64url (no padding) of the JSON array `[account_id, folder_id,
bare_message_id]`. Stable across restarts. A string that does not decode to
that shape, or names another account, is `request.unknown_handle`. Message-IDs
leave the connector in angle-bracket form (`<id>`) and enter the bridge bare.

## 12. Live validation must check first

1. That `X-Chamberlain-Write-Token` survives into the Sent copy on each provider, and
   whether Thunderbird ever keeps a supplied `Message-ID` (it should not).
2. That re-saving a reopened draft replaces the old copy and what Message-ID
   the new copy carries.
3. Read flag stability across `getFull`, attachment reads and moves on Gmail
   and Exchange.
4. Outbox visibility for a queued send.
5. Calendar (never run against a real Thunderbird): that
   `calendar_api.js` loads under the target Thunderbird (`calUtils` and
   `CalIcsParser` module paths); that `icalString` of an occurrence is the
   occurrence and not its series; that `addItem`, `modifyItem` and `deleteItem`
   on an Owl calendar complete and a read-back follows promptly; that
   `icalString` is stable enough to serve as the etag across a restart; and
   whether Owl exposes a last-synchronised time for `fresh_as_of`.
