# Board

The one place builders tell each other things. Read it at the start of every
working session; append to it whenever one of the cases below applies.

## Rules

1. **Append only.** New entries go at the bottom. Never edit or delete an
   entry, including your own; add a follow-up entry instead.
2. **One entry, one subject.** Use this heading form:

   ```
   ## 2026-11-25 14:10 UTC | track | KIND | short subject
   ```

   `track` is `architect`, `memory`, `attention`, `writes`, `thunderbird`
   or a wave-2 name. `KIND` is one of:

   | Kind | Use it when |
   | --- | --- |
   | `CONTRACT-CHANGE` | You changed, or need to change, a shared file (`docs/OWNERSHIP.md`). Name the files, the rule IDs, the reason and who is affected. |
   | `INTERFACE` | You added or changed something in your own code that another track calls or reads (a table column you write, a service method, a module name). |
   | `BLOCKER` | You cannot proceed without another track or the owner. Say exactly what you need and what you are doing meanwhile. |
   | `QUESTION` | You need a ruling on what a contract means. Say how you are reading it meanwhile. |
   | `DECISION` | The answer to a question, or a choice the contract left open. |
   | `HANDOFF` | Your track, or a milestone of it, is done: what exists, the gate result, what is not done, where the next person starts. |
   | `ACK` | You have read an entry that affects you and adapted to it. Quote its heading. |

3. **Additive contract changes** (a new optional field, error code, event
   kind, system key or limit) may land at once: post the entry and commit
   the change in its own commit. **Breaking contract changes** wait for an
   `ACK` from every affected track, or a `DECISION` from the architect or
   owner.
4. **Keep it short and factual**: what, where (paths), since which commit.
   No status diaries. No "still working on it".
5. **Never post private data**: no mail content, addresses, names from the
   owner's data, tokens or paths into `OutlookAutomation-data`.
6. Do not wait on the board. While blocked, work against the fake provider
   and the service doubles (`src/chamberlain/app/doubles.py`).
7. Commit board entries with the change they describe, or on their own;
   resolve merge conflicts in this file by keeping both sides in time order.

## Entries

## 2026-11-24 12:00 UTC | architect | HANDOFF | Wave 0 skeleton and contracts

- Contracts are in `docs/contracts/` (start at `00-index.md`). Briefs for the
  four wave-1 tracks are in `docs/waves/wave-1/`.
- The gate is `python -m chamberlain.dev check` and is green on the skeleton.
- Built and usable now: `schema/`, `domain/` (shared modules), `ports/`,
  `store/` with migration `0001`, `connectors/fake/`, `testing/` (contract
  suites and rigs), `app/` (entry point, service protocols and doubles),
  CLI and MCP stubs.
- Not built: every handler except `doctor`; real `Services`; any real
  connector; the viewer.
- Integration of the four tracks into `app/wiring.py` is a separate step
  after wave 1, done by the architect.

## 2026-11-24 12:00 UTC | architect | DECISION | Choices the plan left open

1. Operations have two states the plan's diagram lacks: `proposed` (intent
   stored, grant pending) and `cancelled` (04-writes.md).
2. `propose` is the only way to attach an in-chat approval
   (`operation` + `approval_evidence`); there is no thirteenth tool. Human
   confirmation is mandatory for standing grants and unknown recipients, and
   is also the default for sends to known recipients; other writes default
   to agent-recorded evidence (05-grants.md, G-12). The owner may revisit
   these defaults.
3. `forget` revokes a grant and cancels an undispatched operation (T-11).
4. Terminal grants and operations are compacted, and the compact rows leave
   with the 730-day audit window. "Never deleted" means never deleted by
   use, revocation or expiry (G-2).
5. Type checker: pyright (strict on `src`). Packaging: setuptools and pip;
   `uv` is not available on the build machine.
6. The MCP stub is a dependency-free JSON-RPC loop. Adopting the official
   SDK is a wave-2 decision.
7. Tracks meet through the SQL schema (one writer per table) and the
   protocols in `app/services.py`; they never import each other
   (`tests/test_layout.py`).
8. Migration numbers are reserved per track (09-storage.md, S-12).

## 2026-11-24 12:00 UTC | architect | DECISION | Owner rulings on wave-0 open points

Coordinator (Opus) relaying owner decisions. Grants: human confirmation in the viewer is
mandatory for standing grants, unknown recipients and attendee calendar writes; one-off
sends to known recipients default to `agent_evidence` (05-grants.md row for
`grants.send_known` changed). Legacy Outlook is not rebuilt. Audience: owner plus friends.
Alpha: breaking changes allowed. All other wave-0 decisions stand until a ruling here.
Wave-1 builders each work in their own worktree and branch (cv2/<track>) with their own
`.venv`; the coordinator merges into `chamberlain-v2` in the order memory, writes,
attention, thunderbird. On merge conflicts in this file keep both sides.

## 2026-11-24 12:40 UTC | architect | DECISION | Review rulings R1-R11 (binding; override the contracts where they differ)

From the plan author's review, ruled by the coordinator. Contract text will be updated at merge; code to these now.
- R1 (writes, thunderbird): `intent_sha256` EXCLUDES harness-assigned tokens (`message_id`, calendar `uid`). The reconcile token is derived deterministically from `sha256(grant_handle + ":" + intent_sha256)`. Thunderbird must PROVE it honours a supplied Message-ID (capability `mail.send.message_id_honoured`); where it does not, reconcile matches as the old code did: envelope (from/to/cc/bcc/subject), normalised body hash and attachment hashes.
- R2 (writes, thunderbird, ports): `ReconcileResult.status` gains `queued` (an exact copy sits in Outbox or Drafts awaiting submission). `queued` keeps the operation `uncertain` and NEVER releases the grant. Any `absence_provable` claim for sends must cover Sent, Outbox and Drafts.
- R3 (writes, thunderbird): `prepare` for `mail.send` runs the duplicate scan over Sent, Drafts and Outbox (normalised subject, body match, recipient overlap). An incomplete scan or a match fails with a conflict error unless the grant scope sets `allow_duplicate`.
- R4 (memory, writes, thunderbird): move, trash and draft-update receipts return the NEW locator; the write engine calls a memory service `refs.repoint(old_locator, new_locator)` on `confirmed`, so handles survive filing. Memory owns `repoint`.
- R5 (writes): operations carry a dispatch lease (`lease_owner` = pid + process start time, `lease_expires_at`). Crash recovery may only touch operations whose lease expired. A late `applied` result arriving after takeover is recorded as reconcile evidence and moves `uncertain -> confirmed` through the reconcile path.
- R6 (writes): immediate settle. After a timeout or unverified result the engine runs bounded read-only reconciles (mail: up to 3 attempts within 30 s; calendar: up to 60 s) before declaring `uncertain` and raising an event.
- R7 (thunderbird, writes): Gmail trash that merges into an existing copy is `applied` with `read_state_preserved: false` and a warning (`merged_existing_copy`), not a failure.
- R8 (thunderbird): in scope: autosave paused in harness-owned composers; a manual edit by the user invalidates the approval; the bound Sent folder re-applied on reopened drafts and replies; `display_only` drafts. `mail.draft_discard` only closes a composer and NEVER deletes saved mail.
- R9 (memory, thunderbird): reserve `bridge/` in the data-root layout (M-25) for the native-host launcher, manifest, add-on package and runtime token; the sweeper never touches it.
- R10 (attention): sessions are per agent AND purpose; a scheduled run and a live chat never supersede each other. Port the old guard explicitly: a scheduled run whose dated local day is not today, with no live user turn, must stop.
- R11 (writes, thunderbird): if the Sent copy is missing but the exact message (token or content match) has arrived in the sender's own Inbox for a self-addressed send, reconcile counts it `present`.

## 2026-11-24 14:45 UTC | architect | DECISION | Memory interim readings accepted; memory merged

All six memory QUESTION readings are accepted as the rule (sessions owned by attention, dead when ended or idle 4 h; hash-matched files never orphans; refs live while linked or named by a non-terminal operation; tombstones 7 d and quarantine rows 14 d after file deletion; evidence of compacted grants/operations dies; forget reports deleted once unreadable). cv2/a-memory is merged into chamberlain-v2 (8ff6bc2). Writes and attention: `repoint` on the RefResolver Protocol arrives with the writes merge; plug compaction into `memory.sweeper.sweep(..., compactors=...)`.

## 2026-11-24 15:00 UTC | architect | DECISION | Wave 1 merged; integration started; Q2 corrected

memory, writes, attention and thunderbird are merged into chamberlain-v2 (gate: 802 passed, 3 skipped). Integration runs on cv2/integration. Writes' interim Q2 is overruled to match the owner: human confirmation only for standing grants, sends to unknown recipients and calendar writes on events WITH attendees; attendee-less calendar create/update/delete and one-off sends to known recipients use agent_evidence. Writes' Q1, Q3, Q4 and Q5 interim readings are accepted. Thunderbird's header rename (X-Chamberlain-Token clash) and QUEUED/warnings adoption are done in integration.

## 2026-11-24 17:30 UTC | architect | DECISION | Integration settled the wave-1 gaps (branch cv2/integration)

- Services and tick: `app/wiring.py` builds memory's services first, then writes' audit and control (given memory's artifact store), installs `writes.maintenance.recover_operations` as the always-step and `memory.sweeper.sweep(..., compactors=writes COMPACTORS)` then `writes.maintenance.maintain` as the extra steps, and passes the configured `WritePolicy` to `make_handlers`. Attention supplies no compactor (it trims its own events and sessions in its tick).
- Thunderbird now returns `ReconcileStatus.QUEUED` and carries R7 in `ProviderReceipt.warnings` / `read_state_preserved`. The bridge write-token header is `X-Chamberlain-Write-Token` (add-on, protocol docs, tests); `X-Chamberlain-Token` stays the viewer secret (G-17).
- `propose` for `mail.send` runs the connector's read-only `prepare` (duplicate scan, R3) before the operation is stored, with a throwaway token and `_allow_duplicate` when the covering grant allows it.
- Owner ruling applied (G-12a): human confirmation only for standing grants, sends to unknown recipients and calendar writes on events with attendees; attendee-less calendar create, update and delete and one-off sends to known recipients use `agent_evidence`. For update and delete `propose` reads the stored event and keeps the fact in the stored refs (`_event_attendees`); an update that lists attendees counts too; standing grants still never cover update or delete. Writes' interim Q2 is replaced.
- Deletion ruling: CalDAV and Owl deletes are real deletes; P-16 read-back accepts cancelled or gone.
- Calendars' QUESTION 1 (no calendar connector sends invitations; attendee writes fail at `prepare` with `capability.unsupported`) stands until wave 2 revisits it; the human-confirmation rule for attendees is ready for a connector that can send them.
- System keys: every key literal in `src/` is in `SYSTEM_KEYS` (a test enforces it); memory's `memory.over_budget` and `memory.last_orphan_scan_at` were already there.
- Contracts updated: 02 (`scheduled_day`, metrics), 04 (W-2, W-9, W-26 to W-32), 05 (G-12a), 06 (`bridge/`, M-28 to M-34), 07 (tick order, R10), 08 (queued, P-12, P-15, P-16), 10 (metrics source), 12 (header).

## 2026-11-24 17:30 UTC | architect | INTERFACE | What integration added for wave 2

- `chamberlain.app.wiring`: `build_app(data_root, *, clock, sources, services, policy, budgets)`, `build_services`, `install_tick_steps(policy, budgets)`, `run_tick(app, force=True) -> (ran, wake_status)`. `App.context` exposes the shared context for operator commands. The config loader that turns the profile into a `SourceRouter`, a `WritePolicy` and `Budgets` is wave 2; the CLI builds an app with no sources until then.
- CLI: `chamberlain tick` (forced; exit 10 when events wait and no session is active) and `chamberlain doctor [--metrics] [--check ...]`.
- `app/metrics.py` fills all sixteen `10-targets.md` keys. `largest_reply_chars_7d` is parsed from the `tool.call` audit summary, now `"<tool>: <outcome> (<n> chars)"` with the size before the cap; keep that format.
- `tests/integration/`: `Stack`/`build_stack` (the wired app over the fake provider, with an optional mail wrapper) and `thunderbird_stack` (the same over the Node-hosted add-on). `Stack.know(...)` seeds known recipients as a confirmed grant would.
- Gate: new `addon` step runs `npm ci` when `node_modules` is missing and `npm run typecheck` in the add-on directory; Node is required (`CHAMBERLAIN_ALLOW_NO_NODE=1` skips). CI already installs Node 22.

## 2026-11-24 17:30 UTC | architect | DECISION | Measured baseline for the budgets of 10-targets.md (replayed mail pass over the fake)

Four unread messages read, carded, two answered with sends to known recipients, two marked read, cards closed, then `next` (31 tool calls, `tests/integration/test_budgets.py`, run with `-s`):
- standing prompt 226 chars (target under 400); 12 tools; guidance corpus 36 rules, 6,929 chars (under 12,000);
- all replies together 17,385 chars, about 4,400 tokens at four characters a token, standing prompt included: the baseline for "at most half" in wave 2;
- largest reply 2,450 chars (cap 6,000); largest guidance in one reply 445 chars (cap 800);
- guidance for the whole pass 1,459 chars against the 1,500 target: 41 characters of room. Any new rule that triggers in a mail pass breaks the test; trim before adding.

## 2026-11-24 17:45 UTC | architect | DECISION | Absent proof waits for the deadline plus a margin; 429 counts as unknown

Coordinator ruling applied in `writes/engine.py` (`_absence_proven`): an `absent` reconcile frees a grant only after `max(SETTLE_AFTER, CONNECTOR_DEADLINE_SECONDS + ABSENCE_MARGIN_SECONDS)` since dispatch (`limits.py`: 30 s plus 120 s margin, inside the 10 minute settle that already applied); before that `absent` keeps the operation `uncertain`. Property test `test_w12_an_absent_proof_never_frees_a_grant_before_deadline_plus_margin`. Accepted as a rule for connectors: a 429 (and 408, 5xx) on a write means it may have happened, so the result is unknown, never `not_applied`.
