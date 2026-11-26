# Wave 2 (sketch)

Starts after wave 1 is integrated: the four tracks wired into
`app/wiring.py` with real `Services`, the tick calling the sweeper and
operation recovery, and one end-to-end scenario over the fake (start, find,
card, keep, propose, confirm, do with a lost response, reconcile, sweep)
passing in the gate. Briefs will be written then; this is the outline.

## Integration (architect, before wave 2)

- Wire `Services`, `HOOKS`, `EXTRA_STEPS` and the source router.
- `doctor --metrics` with every key of `10-targets.md`.
- Golden replies for all twelve tools; replayed sessions to set the token
  and guidance baselines of section 9.
- Decide: official MCP SDK or the built-in loop; add the Node step for the
  add-on to the gate and CI.
- Owner review of the rewritten guidance rules and of the grant
  confirmation defaults (G-12).

## Track (e): calendars

- `connectors/google_calendar/`: Google Calendar API as a first-class
  `CalendarPort`, with its own binding type. OAuth token in the operating
  system credential store (`keyring`), never in a file. Guided setup.
- `connectors/caldav/`: one CalDAV `CalendarPort` for iCloud and other
  standard servers. Contract suite runs against a local Radicale server
  started by the test fixture (a pip dependency in the `dev` group).
- Thunderbird with Owl for Microsoft 365 calendars: narrow capability,
  version handshake, revalidated per release. Check first which Microsoft
  protocol Owl uses on the tenant, against the Exchange Web Services
  retirement.
- All three pass `CalendarPortContract`; `calendar.conflict` and
  `reminder.due` detectors get real data.
- Binds: `08-ports.md`, `04-writes.md`, `09-storage.md` (S-17).

## Track (f): viewer, runs and reports

- `surface/viewer/`: a loopback-only local web page on the new store. It
  mounts the confirmation handler from `writes/confirm.py` and adds the
  UI: pending grants with the exact content to be authorised, grant review,
  read-only uncertain operations in the Timeline, "what the assistant did today" from the
  audit log, cards and due actions, storage figures.
- Scheduled passes as events (`schedule.pass_due`), the session and
  schedule record, `chamberlain tick` for the operating system scheduler,
  and per-agent wake adapters.
- `user.note` events from the viewer; `update.available`.
- Binds: `05-grants.md` (G-17 to G-19), `07-attention.md`, `10-targets.md`.

## Track (g): importer and cutover

- `chamberlain import <old data root>`: one shot, reads the old
  installation and never modifies it. Rules are in the plan's "Migrating
  current users' private data" section. The safety-critical ones: every
  journal entry not in a final state becomes an `uncertain` operation,
  never a retryable one; standing grants import as needing re-confirmation
  in the viewer; grants with no known date are due for review at once
  (G-6); cards over a cap import flagged (M-13); private guidance
  overrides are listed for the owner, not imported.
- Writes only through the tracks' repositories, with actor
  `system:import`, and is idempotent (a second run changes nothing).
- Tests use a synthetic old data root built by a fixture; never the
  owner's data.
- Cutover checklist: parallel read-only run, no period in which both
  systems may write, the old data root left untouched as the fallback.
- Binds: all contracts; `09-storage.md` (S-14).

## Later, not wave 2

Live validation against a disposable Gmail test account (sets capability
levels to `live_verified`), add-on signing, installers, Gmail API and
Microsoft Graph connectors.
