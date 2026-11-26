# 06 Memory system

Principle 2: the agent receives only what it needs, in bounded pieces;
everything stored has an owner and an end date; the harness erases it when
the owner is finished. Binding for the memory track; read by all.
Code: `src/chamberlain/memory/`, numbers in `src/chamberlain/limits.py`.

## Data classes

Every table and every file under the data root belongs to exactly one class.

| Class | What | Lifetime rule |
| --- | --- | --- |
| Records | Cards, actions, grants, operations, sources | Live until closed or terminal; then compacted to an outcome line; the line is deleted after its retention. |
| Evidence | Small immutable statements | Deleted when its last referrer is deleted or compacted. |
| Working set | Notes, artifacts (cached bodies, attachments, downloads, imports), refs | Owned. Deleted when the last owner ends or `expires_at` passes, whichever is first. |
| Ephemeral | Sessions, cursors, leases, guidance deliveries, events | Hours, or end of session. |
| System | Audit log, backups, logs, quarantine, migrations | Fixed slots and fixed windows. |

- **M-1** There is no sixth class and no table without a class. The
  mapping lives in `store/schema.py` (`TABLE_CLASSES`) and a test fails when
  a migration adds a table that is not in it.
- **M-2** There is no key-value `meta` table. `system_state` holds a closed
  set of keys listed in code; writing an unknown key raises.

### Lifetime table

| Item | Limit | Then |
| --- | --- | --- |
| Closed card | at close | Compact to `outcome` (240 chars); delete position, goal, rules, closed actions' notes |
| Card outcome line | 365 days | Delete row |
| Finished action | at finish | Notes deleted, artifacts released; outcome line 90 days |
| Note | owner action open | Deleted with the action; hard cap 30 days untouched |
| Evidence | while referenced | Deleted at once when unreferenced |
| Artifact | owner live and before `expires_at` | Released; bytes deleted when no owner remains |
| Artifact default expiry | 30 days; maximum 180 days | An open action renews its artifacts on review, never automatically |
| Context carried over from the old harness | 60 days (`IMPORTED_CONTEXT_DAYS`), or until the agent forgets it, or the card closes | Released, bytes deleted. Never renewed; never brought back by a later import (M-35) |
| Larger-limit request (`complex_cards`) | until the card closes | Deleted with the card. A declined row is kept, so the 30-day wait holds (M-37) |
| Session-owned artifact | session end or 24 hours | Deleted |
| Ref (`msg`, `cev`, `con`) | 30 days after last seen, unless referenced by a live record | Delete row |
| Session | idle 4 hours | Ended; its ephemeral rows deleted |
| Cursor, lease | 2 hours, 30 minutes | Deleted; leased file removed |
| Mail watermark | per source, until the source is removed | Kept (a record) |
| Mail folder sync (`mail_sync`, `mail_sync_folders`) | per source, until the source is removed; a folder row goes when the folder is gone or no longer read | Kept (a record) |
| Owl freshness (`owl_freshness`) | per source and binding | Keep pending probe tokens until safe cleanup; replace a completed record on the next probe |
| Seen mail | triaged and older than the overlap behind the watermark | Deleted (untriaged rows are never deleted) |
| Schedule | until the person removes it | Kept (a record) |
| Run | 35 days after its day | Deleted by the sweeper's records step (`runs_trimmed`); its report artifacts follow their own `expires_at` (30 days by default) |
| Report outline | 72 hours, kept under a running pass | A stored item the harness writes; not a report (M-39) |
| Pass report, harness part (`run_reports`) | with its run, 35 days after the run's day | Deleted with the run row (cascade) |
| Local event (`local_events`) | 365 days after it ends | Deleted (`local_events_trimmed`) |
| Event label (`event_labels`) | 365 days after the event ends | Deleted (`event_labels_trimmed`) |
| Improvisation pattern (`improv_patterns`) | 365 days after last seen; at most 500 kept | Oldest-seen dropped (`review_pruned`, M-40) |
| Transcript position (`improv_files`) | 120 days after last seen | Deleted (`review_pruned`) |
| Review round and its findings | 365 days after the round opened | Deleted, findings with it (`review_pruned`) |
| User note | 7 days open, 14 days kept | Stops raising `user.note` after 7 days; deleted after 14 (`notes_trimmed`) |
| Event | condition true | `expired` when false; row deleted 14 days after terminal |
| Operation | 30 days after terminal | Compacted (W-25) |
| Terminal grant | 90 days | Compacted (G-2) |
| Audit log, compact operations and grants | 730 days | Removed in whole months with a checkpoint (G-22) |
| Backups | 2 daily + 2 weekly slots | Oldest overwritten |
| Logs | 14 days, 20 MB | Rotated |
| Quarantined orphan | 14 days | Deleted |
| Metrics history (`memory.metrics_history`) | one snapshot a week, 30 kept | Oldest dropped |

- **M-3** Each line above is implemented once, in the sweeper's rule table.
  No other module deletes rows or files for lifetime reasons.

## Artifacts and owners

An artifact row: `handle`, `kind` (`mail_body`, `attachment`, `download`,
`import`, `text`), `media_type`, `size`, `sha256`, `filename?`, `reason`,
`created_at`, `expires_at`, `state` (`live`, `released`, `deleted`).
`artifact_owners` links it to one or more owners.

- **M-4** No owner, no storage. `keep` without a live owner fails with
  `artifact.no_owner` or `artifact.owner_ended`. Owner kinds and liveness:

  | Owner | Live while |
  | --- | --- |
  | action | `state = open` |
  | card | `state = open`. Evidence, and the one stored text item the importer keeps (M-35). The agent cannot name a card as an owner of `keep`; artifacts it stores are owned by actions |
  | operation | not terminal, plus 24 hours after terminal |
  | grant | `proposed`, `active` or `suspended` |
  | session | not ended |
  | run | its row exists (rows live 35 days past their day; the report's own `expires_at` still ends it first) |

- **M-5** Liveness is read from the owning track's tables as defined in
  `09-storage.md`. The memory track never writes those tables.
- **M-6** Bytes live in one content-addressed folder,
  `<data root>/artifacts/<aa>/<sha256>`, written atomically (temporary file,
  hash verified, rename). Identical content is stored once and shared by
  rows. The agent never sees or chooses a path there.
- **M-7** A stored hash is verified on every read. A mismatch marks the
  artifact `deleted`, raises `artifact.hash_mismatch` and never returns the
  bytes. A failed or interrupted write leaves neither a partial nor a final
  file.
- **M-8** Paths supplied to `keep.import_path` are resolved and checked: the
  file must exist, be a regular file, not a symlink escaping its parent, and
  be under `IMPORT_MAX_BYTES` (25 MB). It is copied; the original is not
  touched.
- **M-9** `open` with `lease: true` copies the artifact to
  `<data root>/leases/<random>/<filename>`, returns that path and an expiry
  (30 minutes). The sweeper deletes it. Leased paths are for handing a file
  to the user or another program; edits there do not reach the store.
- **M-10** Releasing: finishing or closing the owner releases its hold.
  `forget` releases early. When the last hold goes, the row is `released`
  and the sweeper deletes the bytes if no other live row has that hash.
- **M-11** An artifact attached to a non-terminal operation (a pending send)
  survives any clean-up, even past `expires_at`, until the operation is
  terminal (compare G-10).

## Bounded records

| Field | Cap (characters) |
| --- | --- |
| Card `title` | 120 |
| Card `goal` | 400 |
| Card `position` | 1,500 |
| Card `rules` | 600 |
| Open actions per card | 7 |
| Action `title` | 160 |
| Note `text` | 1,000; 5 notes per action |
| Evidence `text` | 2,000; 15 per open card (target) |
| Outcome line | 240 |
| Whole open card, serialised | 3,000 |

- **M-12** A write that would exceed a cap is refused (`card.over_cap`,
  `note.over_cap`, `evidence.over_cap`) with a `fix` naming the field, its
  size and its limit. Nothing is truncated silently.
- **M-13** Imported cards over a cap are stored as they are, flagged
  `over_cap`, and raise `card.review_due`; their next update must bring them
  under.
- **M-14** Cards hold current state only. Updating replaces fields; there is
  no append, diary or history on the card. History is the audit log.
- **M-15** Evidence is immutable and MUST NOT hold a mail body, a card dump
  or an attachment. It holds a statement and, optionally, a ref handle and
  the hash of what it describes. An expired source cannot be re-used as new
  authority: evidence whose referrers are gone is gone.
- **M-16** Card updates carry `expected_version`; a stale one fails with
  `card.version_conflict`.

### Larger limits for a complex card

The normal caps above stay. For a genuinely demanding card the agent may ask, and
the owner may agree, to raise two of them for that one card:

| Field | Normal | Complex card |
| --- | --- | --- |
| Card `position` | 1,500 | 4,000 |
| Whole open card, serialised | 3,000 | 6,000 |

- **M-36** The agent asks with `card` (`op: update`, `complex_reason`, no
  `fields`). That records a `pending` request and changes nothing else. Only the
  owner, in the viewer, moves it to `approved` or `declined`; no tool can. While a
  card is `approved`, M-12 uses the larger caps for it. Everything else
  (`title`, `goal`, `rules`, actions, notes) keeps its normal cap. Every request,
  approval, decline and revoke is one audit row (`card.complex_requested`,
  `card.complex_approved`, `card.complex_declined`, `card.complex_revoked`).
  The owner's rows name `human:viewer`; a request names the agent's session.
- **M-37** There is no limit on how many cards are `approved`: the owner's
  click is the control (owner ruling 2026-11-24). A declined card may not ask again for 30 days (`COMPLEX_DECLINE_DAYS`); the
  refusal gives the date. The agent is told of an approval or decline by a
  `card.review_due` event (reason `complex_approved` or `complex_declined`) for
  7 days, and `open` shows `complex` on the card (`pending`, `approved`,
  `declined`). Closing the card ends the request.
- **M-38** Revoking returns the card to the normal caps at once. If it is then
  over them, it is flagged `over_cap` and due for review now, and the ordinary
  over-cap flow (M-13) applies: its next update must bring it under. Nothing is
  cut. The size of a card never counts `context` or `complex`, which only
  describe it.

## Retrieval

- **M-17** Every list is paged with a character budget (`PAGE_CHARS`,
  4,000) and a cursor. Cursors are opaque, bound to the session and the
  query, immutable (a continuation sees the same collection it started
  with), and expire after 2 hours (`cursor.expired`).
- **M-18** Lists return handles and one-line summaries, never bodies.
  `open` returns one bounded range. `find` with `within` returns matching
  lines and offsets.
- **M-19** Default projections are minimal; more is an explicit `part`.
- **M-20** Mail is read live by reference. A body or attachment is cached
  as an artifact only through `keep` with an owner. Distinct physical copies
  of one RFC message (same `Message-ID`, different folders) keep distinct
  refs; they deduplicate only as bytes.

## The sweeper

```python
def sweep(ctx, now) -> SweepReport
```

- **M-21** One function, in `memory/sweeper.py`, applies the lifetime table.
  It runs on every tick, is idempotent, is safe to interrupt at any point
  (a second run finishes what the first began), and returns a report of
  counts and bytes per rule, which is audited as one `sweep` row.
- **M-22** Order within a sweep: end idle sessions; compute live owners;
  release holds of dead owners; expire artifacts; delete unreferenced
  evidence; delete bytes with no live row; compact records; trim ephemeral
  rows; scan for orphans; rotate system files.
- **M-23** A deletion that fails (file locked) is recorded and retried next
  sweep; it is reported in `doctor`, never ignored.
- **M-24** Backups MUST exclude the artifact folder's released content and
  MUST NOT be able to revive an expired artifact, a closed card's body or a
  compacted record: restore replays the sweeper before the store is used.

## Orphans and quarantine

- **M-25** The data root has a fixed layout: `chamberlain.sqlite3` (and its
  `-wal`, `-shm`), `config/`, `artifacts/`, `leases/`, `backups/`, `logs/`,
  `quarantine/`, `run/`, `bridge/` (R9: the native-host launcher, manifest,
  add-on package and runtime token; the sweeper never scans it), `import/`
  (the importer's `report.json`, which the owner reads) and `secrets/` (the
  file-store fallback for secrets); the sweeper never scans these either. Anything else
  under the data root, and any file in
  `artifacts/` with no live row, is an orphan.
- **M-26** The sweeper moves orphans to `quarantine/<date>/`, records them
  in the `quarantine` table, raises one `storage.orphans` event per sweep
  that found any, and deletes them after 14 days. It never follows a symlink
  out of the data root and never touches anything outside it.

## Budgets

- **M-27** Limits: 20 MB of artifacts per card, 500 MB in total, both
  configurable downward or upward in private settings. Passing either raises
  `storage.over_budget` with the largest releasable handles; `keep` beyond
  the global limit is refused with the same code.

## Property tests that define done

Random sequences of create, own, keep, finish, close, expire, sweep and
crash-mid-sweep, checking after each sweep:

1. nothing owned by a live owner is deleted;
2. nothing survives past its limit without a live owner;
3. no file exists in `artifacts/` without a live row, and no live row lacks
   its file;
4. sweeping twice changes nothing the second time.

## Readings accepted as rules (board, 2026-11-24)

- **M-28** Sessions belong to attention. Memory never writes `sessions`; a
  session is a dead owner when `ended_at` is set or `last_seen_at` is 4 hours
  old, and memory deletes its own cursors for such sessions.
- **M-29** A file in `artifacts/` whose hash matches a non-deleted row is never
  an orphan (rule `bytes` deletes it); only files with no row are quarantined.
- **M-30** Refs are kept while `card_links`, `evidence.ref` or a non-terminal
  operation names them.
- **M-31** Tombstone rows of deleted artifacts live 7 days, so `open` can say
  `artifact.expired`; quarantine rows stay 14 days after the file is deleted.
- **M-32** Compaction of operations and grants, and grant timers, are the
  writes track's rules for its own tables. They run inside the sweeper's
  `records` step through the `compactors` seam (`sweep(ctx, now, compactors=...)`,
  wired in `app/wiring.py`) and are counted in the one `sweep` audit row. The
  evidence of a compacted grant or operation dies with it. Attention trims its
  own events and sessions inside its tick and supplies no compactor.
- **M-33** `forget` on an artifact reports `deleted: true` once it is no longer
  readable; the sweeper removes the bytes later. `RefService.repoint` (R4)
  keeps the handle and replaces the locator after a move or draft update.
- **M-34** `con_` handles open to the contact's name and addresses, read live
  with `ContactsPort.read`; an unreachable source is an error, never a stale view.
- **M-35** Context carried over from the old harness. `chamberlain import` stores
  each open old case's free-text `context` (up to 6,000 characters in the old
  harness) as one text artifact owned by the new card. Its `reason` says what it
  is ("Context from the old harness...") and its name is `context-from-old-harness.txt`.
  The card is made due for review now; while it holds the item its review event has
  reason `context` (not `review`), so the agent is not nudged to close it before
  the notes are used. A repeat import adds the item to cards that
  already exist and lack it, never twice: the `imported_context` table records each
  old case once, so it also stays gone after the agent has forgotten it. Cards the
  owner has closed since get nothing. The item expires 60 days after import
  (`IMPORTED_CONTEXT_DAYS`) and is never renewed. `open` on the card shows its
  handle as `context`, and the rule `card.context` tells the agent to fold what
  still matters into the position (within the cap) and then `forget` it. The
  import report counts them as `context_artifacts`.

### The improvisation log and the harness review (track r-review)

- **M-40** After each scheduled pass, and otherwise at most once a day, the harness reads the
  agent's own session transcripts (Claude `projects/*/*.jsonl`, Codex `sessions/**/*.jsonl`; the
  folders come from `review/homes.py`, never guessed in tests) for sessions that ran in this
  installation's checkout or called a Chamberlain tool. For every call that is not a Chamberlain
  tool it records ONLY the tool name and, for a shell command, a short normalised summary (the
  program and at most one sub-command word from a short list; never an argument, path, quote or
  number). It never reads or keeps outputs, mail text or file contents, and keeps no path,
  session id or project name. Counts are aggregated by pattern. The log is not in the daily report
  and not on the viewer's front page. How often depends on how much is new: once
  `review.new_patterns` (default 5) patterns were added since the last round and at least
  `review.min_days` (3) have passed, or after `review.max_days` (60) if any pattern was added,
  the harness opens a round and raises
  `harness.review` (07, A-1c). Findings go in with `keep` kind `finding` (02, T-10c). A worrying
  finding appears once as a line in the next pass report; ideas stay in the store for the
  coordinator.

### The pass report (track d-dashboard)

The written report is the owner's front page in the viewer. Three parts make it:

- **The layout.** `runs/report_default.yaml` ships in Git; the owner's own layout is the one private
  file `config/report-template.yaml` (written by `chamberlain import` from the old report template,
  never overwritten). It lists sections in order. Each is written by the harness or by the agent,
  and has a heading and a style (`plain`, `red` or `orange`).
- **The harness part.** When a pass settles (a report is kept, or its session ended without one) the
  harness writes the sections it can from its own records, and keeps them in `run_reports`: access
  problems per source, important appointments, permission reviews due, and verification (newest
  mail, how far it was read, calendar freshness, filing counts). They are facts as of that moment.
- **The agent part.** The agent keeps its text with `keep` (`kind: artifact`, `owner` the run). It is
  never copied into the database; it is read from the stored item (hash-checked) and expires with it.

- **M-39** While a pass is `running` the harness keeps one stored item under the run, named
  `report-outline.txt` (72 hours), that says which sections are the agent's and the owner's wording
  for each. It is not a report: readers of a run's reports leave it out, and it never finishes a run.
  The viewer draws the callouts from the layout's `style`, so text from the agent or from an email can
  never pose as one. An important appointment is an open action on an open card with `important`
  set (migration 0140; the agent decides from the card's rules, the context and its general rules,
  owner ruling 2026-11-24), due at the appointment's start, snoozed until its reminder shows. The owner
  acknowledges a waiting reminder in the viewer (`POST /api/appointments/<action>/acknowledge`, the
  viewer's usual token and Origin checks); that finishes the action and writes an audit row
  (`appointment.acknowledged`) whose actor is `human:viewer`. No tool reaches it. Local events and
  their removal are audited the same way (`local_event.added`, `local_event.removed`).

### Carrying the owner's old rules across (track f-import)

- **M-41** `chamberlain import` carries more than cards. Each item is written once and recorded in
  `imported_items` (kind, old id), so a repeat import adds only what is missing, never overwrites a
  change made in v2 since, and never brings back what the owner removed.
  1. *Card rules.* An open old card's own instructions (its `customisations.instructions`) and its
     per-card decisions become the new card's `rules` (decisions as "Decided: ..." lines). Over the
     600 character cap the card is flagged and due for review (M-13). On a card that already exists
     the rules are filled in only while they are still empty.
  2. *Important appointments.* An old action marked important (its flag, `-important-` in its id, or
     a next step that starts "IMPORTANT:") gets `important = 1`; its due time is the appointment's
     start and its snooze time the old reminder time. A repeat import changes an existing action
     only while it is open, not yet important and still has the due time the first import gave it.
  3. *Owner instructions, standing facts and keyword watches* go to the private file
     `config/owner-instructions.yaml` (07, A-28). A topic already in the file is never replaced.
  4. *The old read-state settings* become profile options (docs/configuration.md, `read_state`) and
     a proposed standing grant per mailbox (05, G-24).
  Not carried: the weekly sign-in notice roundup (docs/cutover.md).
