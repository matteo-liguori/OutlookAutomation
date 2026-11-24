# 07 Attention system

Principle 1: the agent knows only that the harness exists and how to call
it; the harness notices what is relevant and hands over just enough guidance
at that moment. Binding for the attention track; read by all.
Code: `src/chamberlain/attention/`, rules in `src/chamberlain/guidance/`.

## Events

An event row: `handle`, `kind`, `subject` (a handle or source ID),
`dedupe_key`, `priority` (0 to 100), `summary` (harness text, 160),
`attrs_json` (small typed attributes that rule filters match, such as
`intent_kind`), `state`, `detected_at`, `delivered_at`, `delivered_to`
(session), `closed_at` (when it became handled or expired), `quiet_until`
(A-3).

Kinds (closed enumeration `EventKind`):

`mail.new_for_card`, `mail.new_unmatched`, `action.due`, `reminder.due`,
`card.review_due`, `grant.needed`, `grant.review_due`,
`operation.uncertain`, `send.missing`, `source.stale`, `source.sign_in_needed`,
`calendar.conflict`, `user.note`, `storage.over_budget`, `storage.orphans`,
`update.available`, `schedule.pass_due`, `report.due`.

States: `new -> delivered -> handled`, and `new | delivered -> expired`.

- **A-1b** `report.due` is the last item of a scheduled pass: the run is
  `running` (a scheduled session works it), no report is kept yet and the
  harness has stored the report outline. Its subject is the outline item (06,
  M-39), its priority is 5 so every other item comes first, it is never a
  notice, and it expires when a report is kept. The rule `report.write` rides
  with it.
- **A-1** `dedupe_key` is `kind + ":" + subject` plus, where one subject can
  have several distinct instances, a discriminator fixed per kind. A
  condition detected twice is one row: at most one non-terminal event exists
  per key (a unique partial index enforces it).
- **A-2** Events that stop being true expire by themselves: each tick, a
  non-terminal event whose detector no longer reports its key becomes
  `expired`. Delivery never makes an event go away; only `handled`
  (the agent listed it in `next.handled`) or `expired` does.
- **A-3** A handled event whose condition is still true MUST NOT reappear
  until the condition has been false once, or its kind's re-raise interval
  passes (`operation.uncertain`: 24 hours; `grant.review_due`: 7 days;
  others: never).
- **A-4** `operation.uncertain`, `send.missing` and `source.sign_in_needed` cannot be marked
  handled by the agent; they close only when the condition ends.
- **A-4a** `send.missing` (priority 94) is raised for an uncertain mail send once the harness's
  24-hour look (04-writes.md W-37) found it missing (`reason: missing`) or waiting in the Outbox
  (`reason: outbox`); the operation then no longer raises `operation.uncertain`. A change of
  answer is a new event (the discriminator is the result). It stays until the send is settled.

## Detectors

```python
Detector = Callable[[DetectorInput], Iterable[EventDraft]]
```

- **A-5** A detector is a pure function of `DetectorInput` (read-only store
  queries, connector deltas already collected, `now`). It writes nothing and
  calls no connector. One detector per kind, registered in
  `attention/detectors/__init__.py`.
- **A-6** A tick: collect connector deltas (bounded, read-only, P-3), run
  all detectors, reconcile the inbox (A-1, A-2), run the sweeper (M-21), then
  write maintenance. Crashed-operation recovery (W-9) is an `ALWAYS_STEPS`
  entry: it runs on every call, even a skipped tick, and BEFORE detection, so a
  recovered operation raises `operation.uncertain` in the same call. The sweeper
  and `writes.maintenance.maintain` are `EXTRA_STEPS` run after the inbox is
  reconciled, at most once per interval; `app/wiring.py` installs all three. A
  tick runs at the start of every tool call and when the scheduler calls
  `chamberlain tick` (forced; exit status 10 when events wait and no session is
  active). A tick that ran less than `TICK_MIN_INTERVAL` (60 seconds) ago is
  skipped, except for the always-steps.
- **A-7** A tick MUST NOT fail a tool call. A detector or source that
  raises is logged, shows as a coverage gap or a `source.stale` event, and
  the tool proceeds.
- **A-8** Detectors read other tracks' tables as defined in `09-storage.md`
  and never write them.

### New mail (watermark and triage)

- **A-20** Each mail source has a covered-through watermark (`mail_watermarks`). A tick
  scans the inbox from a little before it (`MAIL_OVERLAP`), whatever the read state, pages
  until the connector says it is complete, and only then moves the watermark to the scan's
  start. A scan that cannot finish in one tick (page budget, connector gap, failure, stale
  health) keeps what it found, resumes below the oldest date reached, and raises
  `source.stale` naming the uncovered span. Mail read on another device is still seen once.
  A source never watched starts `MAIL_BOOTSTRAP` back; `watching_since` records it. Known
  residue: mail whose Date header is older than the required overlap (1 hour) and that
  arrives after a scan that could not finish the overlap.
- **A-21** New messages are stored once (`mail_seen`: identity, time, suggested card; no
  sender or subject). The detector raises one `mail.new_unmatched` event per source, keyed by
  the oldest waiting row, whose next call is a `find` of the inbox since then. Handling the
  event triages only the rows shown with it; mail that arrived later raises a fresh event.
  `mail.new_for_card` is retired as a trigger. A mail event no longer ends because the
  message was marked read; it ends when the agent handles it.
- **A-22** The harness only suggests a card (reply headers first, then a sender who
  corresponds with exactly one open card). It MUST NOT write `card_links`; the agent links.

## Delivery

1. **Pull.** `start` and `next` return the highest-priority non-terminal
   events as `WorkItem`s: handle, kind, one line, one exact tool call.
2. **Piggyback.** Every reply may carry up to 3 `notices` for events of
   priority 80 or more that this session has not been shown.
3. **Wake.** `chamberlain tick` exits with status 10 when undelivered events
   exist and no session is active; a per-agent wake adapter may act on that.
   The harness works without one.

- **A-9** `start` returns at most 5 work items; `next` at most 10. Neither
  returns bodies. A work item's `next` MUST be a valid call (E-4).
- **A-10** A session is a row: `handle`, `agent`, `purpose`, `started_at`,
  `last_seen_at`, `ended_at`. It ends after 4 idle hours or when a new
  `start` supersedes it for the same agent label. Ending a session deletes
  its guidance deliveries, cursors and session-owned artifacts (M-3). The
  schedule and session record held here replaces any reliance on chat
  titles. Sessions are keyed by agent label AND purpose: a scheduled run and a
  live chat never supersede each other (R10). A scheduled session whose local
  start day is not today (`StartRequest.scheduled_day`, given by the wake
  adapter) with no live user turn is stopped on every tool with
  `session.stale`, the port of the old wrong-day guard (`Hooks.guard`).

## Guidance rules

One YAML file per rule in `src/chamberlain/guidance/`, named `<id>.yaml`:

```yaml
id: send.uncertain
version: 3
title: A send may have gone out
when: {event: operation.uncertain, intent_kind: mail.send}
text: >
  This send may have gone out. Do not send again. Call reconcile with the
  operation handle; if it stays uncertain, tell the user what is known.
max_chars: 400
once_per: operation
priority: 90
```

| Field | Rule |
| --- | --- |
| `id` | `[a-z0-9_.]{3,40}`, unique, equals the file name. |
| `version` | Integer, raised on any change to `text` or `when`. |
| `title` | At most 60 characters; shown in the `guide` index. |
| `when` | Exactly one trigger key (below) plus optional equality filters. |
| `text` | Judgment only: when, whether, what to tell the user. |
| `max_chars` | At most 600. `len(text) <= max_chars`. |
| `once_per` | `session`, `card`, `operation`, `event` or `always`. |
| `priority` | 0 to 100. |

Triggers: `event: <EventKind>` (the reply delivers or concerns such an
event); `tool: <name>` (that tool is being called); `result: <path> <op>
<value>` (a condition on the reply, for example
`coverage.complete == false`); `error: <code or category>`.

- **A-11** Guidance MUST NOT contain command syntax, field names, JSON,
  code or example calls. Syntax comes from the schema and from errors
  (E-13). A test rejects rule text containing `{`, backticks around tool
  arguments, `--` flags or any request field name followed by a colon.
- **A-12** Sizes are enforced in the gate: each rule within its `max_chars`
  and within `RULE_MAX_CHARS` (600); all rules together within
  `GUIDANCE_TOTAL_CHARS` (12,000).
- **A-13** At least 90 percent of rules MUST have a trigger other than
  being fetched with `guide`; a rule with no possible trigger fails the
  gate.
- **A-14** Rules mention only the nouns of `01-domain.md` (D-1).

### Selection, per reply

1. Collect rules whose trigger matches this tool call, reply or event.
2. Drop any already delivered in this session at this version for the same
   `once_per` scope key (`guidance_deliveries`).
3. Rank by priority, then ID.
4. Fit whole rules into `GUIDANCE_REPLY_CHARS` (800). Never cut a rule.
5. For each rule that does not fit, emit a pointer `{id, version, more:
   "guide <id>"}` (pointers cost 60 characters of budget each; at most 3).
6. Record deliveries of full texts only. A pointer is not a delivery.

- **A-15** Selection is deterministic: same state, call and reply give the
  same rule IDs in the same order.
- **A-16** A rule with priority 90 or more that does not fit displaces
  lower-priority rules rather than becoming a pointer.
- **A-17** `guide <id>` returns the full text regardless of the ledger and
  records a delivery.

### Overrides

A private override is a patch file in `<data root>/config/guidance/`:
`{id, base_version, op: append | replace | disable, text?}`.

- **A-18** An override applies to one rule. There is no wholesale copy.
  When `base_version` is behind the shared rule's version, the override
  still applies and `doctor --check guidance` reports it for re-checking.
  The patched text obeys the same size and content rules.

## The decorator

- **A-19** The app calls `attention.decorate(ctx, tool, request, reply)`
  once per reply, after the handler. It may set only `next` (when the
  handler left it empty), `guidance` and `notices`. It MUST NOT change
  `ok`, `data`, `coverage` or `error`, and it MUST keep the reply within
  `MAX_REPLY_CHARS` by dropping guidance to pointers before anything else.

## Tests that define done

Scenario tests: given this store state and this tool call, exactly these
rule IDs and these notices are delivered. Budget tests for A-12 and A-13.
Inbox property tests for A-1 to A-3 (random detect, deliver, handle,
condition-clears sequences never produce two live events for one key and
never lose a true condition).
