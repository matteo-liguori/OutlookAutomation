# Switchover runbook: v1 to Chamberlain v2

For an agent, with the owner. Each step says what the agent does, what needs the owner, and when
computer control helps. v1's data folder is the fallback until the archive step, and exactly one
system writes at any time. The commands are in the [agent setup guide](setup-agent.md) and
`chamberlain help`. Pass `--data ROOT` (the new, empty data root, outside the old one) before each
`migrate` command, or set `CHAMBERLAIN_DATA`.

`migrate switch` (for the app automations and the v1 add-on) and `migrate archive` only show
their plan until `--apply`. Everything they change is backed up, and each can be repeated safely.

## 1. Detect

`chamberlain migrate detect OLD_ROOT [--old-checkout CHECKOUT]`

- Agent: run it. It reports the v1 schema and counts, and lists the agent-app automations that
  run v1: Codex `automation.toml` files whose `cwds` include the v1 checkout, and Claude
  scheduled tasks if their files can be found. Each has a name, status and schedule. The checkout
  is found next to `OLD_ROOT` (`<name>-data` beside `<name>`); pass `--old-checkout` otherwise.
- Owner: confirm the list is every v1 automation. Claude scheduled tasks that cannot be read are
  named by the command and must be checked in the Claude app.

## 2. Close v1

- Agent: stop v1's dashboard, native host and any `-m monitor` run. Close Thunderbird with
  `chamberlain thunderbird close`. Do not pause the automations yet.
- Owner: nothing, unless a v1 window needs an answer.
- Computer control: use it to close a v1 window that does not stop from the command line.

## 3. Prepare

`chamberlain migrate prepare OLD_ROOT [--old-checkout CHECKOUT]`

- Agent: run it. It refuses while v1 processes from the old checkout run, and says which to stop.
  Otherwise it snapshots v1's inputs to `ROOT/import/v1-backup` and imports them. (An older
  `ROOT.v1-backup` beside the data root is still read when resuming.) It checks the saved iCloud
  app password, and the iCloud calendars come up enabled. Importing again is safe: it adds only
  what is missing and never overwrites a later v2 change.
- Owner: nothing.

## 4. Review the report

Read the summary and `ROOT/import/report.json`. It is private: do not paste it into chat.

- Agent: report the counts, `skipped_sources`, `warnings`, `standing_grants_need_reconfirmation`,
  `over_cap_cards`, `instructions_to_review` and what was left behind. If iCloud was not
  connected, the report names the one command, `chamberlain setup icloud register --apple-id
  EMAIL`. Run it when the owner has the app-specific password ready.
- Owner: decide about skipped sources. Those bound to Outlook COM or without a folder id are
  listed; bind them in Thunderbird and add them to the profile by hand.

## 5. Profile and instructions

- Agent: run `chamberlain config validate`. Open `config/owner-instructions.yaml`. For each topic
  in `instructions_to_review`, show the owner its text and remove or rewrite it: it names a
  command that only v1 had. Trim cards flagged over their size cap when next updated.
- Owner: approve each change to their own instructions.

## 6. Thunderbird

- Agent: `chamberlain setup thunderbird --manifest-dir MANIFEST_DIR --register` writes the
  native-host manifest and registers it (Windows registry, or the Mac manifest location). Install
  the add-on once through the Add-ons Manager; later updates use `chamberlain addon update`.
  `chamberlain thunderbird close|start` closes and starts Thunderbird for these steps. Then check
  the Inbox, Sent, Drafts and Trash folders as in the setup guide.
- Owner: any sign-in in Thunderbird, and the folder choice if it is ambiguous.
- Computer control: recommended for the Add-ons Manager and account screens.

## 7. Connect the agent and install the schedule

- Agent: `chamberlain setup agent codex --apply` or `chamberlain setup agent claude --apply`
  writes the MCP entry after a backup and checks that it answers. `chamberlain schedule install
  --apply` registers the hidden hourly tick, plus one run before each pass. `chamberlain schedule prompts` prints the
  agent-app automation prompts for the profile's pass times.
- Owner: create the agent-app automations from those prompts if the app gives the agent no way to
  create them. Leave them paused until step 10.
- Computer control: recommended to fill in the automation form in the agent app.

## 8. Recreate permissions

v1's standing grants arrive suspended, with their scope as free text. They authorise nothing.

- Agent: run `chamberlain migrate grants OLD_ROOT`. It lists each live old standing grant with its
  title, source, old wording and a proposed v2 scope. Show the old wording to the owner as
  evidence, map each grant to a v2 standing scope (account, kind of action, exact addresses or
  folders, a daily limit; see [Permissions](permissions.md)), and propose it. Skip grants marked
  `obsolete_in_v2` (delivery tests and development work: self-test authority and the live test
  cover those) or `expired_in_v1`.
- Owner: confirm each wanted permission in the viewer. Only the owner's click activates one.

## 9. Supervised read-only pass

- Agent: run `chamberlain migrate verify OLD_ROOT`, then supervise a read-only pass as in the
  [scheduled pass procedure](scheduling.md). Compare its briefing with v1's. Do not confirm
  grants or write anything: v1 still does all the writing.
- Owner: judge whether the briefing matches what v1 showed.

## 10. Switch

`chamberlain migrate switch OLD_ROOT --pause-app-automations` shows the plan. Add `--apply` to act.
It needs a successful `migrate verify`.

- Agent: pause the v1 app automations. For Codex it backs up each file and sets
  `status = "PAUSED"`; then tell the owner to reload the Codex app. Anything it cannot pause is
  printed by name. It also retires v1's Thunderbird add-on and native host, keeping both for
  rollback: it closes Thunderbird, exports the registry key `outlookautomation_thunderbird_pilot`,
  removes it, and moves `mail-pilot@outlookautomation.local.xpi` out of the profile into
  `ROOT/import/v1-retired`. Pass `--keep-v1-addon` to skip that. If v1 also has a Windows
  scheduled task, add `--old-task` and the new-task options (see `migrate switch --help`). Then
  unpause v2's automations from step 7.
- Owner: reload the agent app, unpause the v2 automations and start Thunderbird.
- Computer control: recommended for any v1 automation in the Claude app that has no file to edit.

## 11. Archive with rollback

Do this after v2 has run for as many days as the owner wants.

`chamberlain migrate archive OLD_ROOT [--keep-days 30] [--also PATH...] [--in-use PATH...]`

- Agent: show the plan, then run it with `--apply`. It moves the v1 checkout, the v1 data folder
  and the import backup (and anything named with `--also`) into
  `<parent>/_archive/<date>-pre-v2-switch`. It copies `v1-retired` and the app-automation backups
  there, writes `ROLLBACK.md`, and registers a one-time deletion task after the keep days
  (Windows; on Mac it prints the command to run). A folder in use, such as the agent's own
  working folder, is moved at the next logon or 03:00 by a one-time task instead; name it with
  `--in-use`. The migration's backup moves too, so `migrate verify` no longer works: archive last.
  If you switched by hand, pass `--confirm-switched`.
- Owner: confirm the archive and the deletion date. To keep it longer, disable the cleanup task.

## Rolling back

Follow `ROLLBACK.md` in the archive. Before the archive step, stop v2 (the tick and its
automations), resume v1's automations (`status = "ACTIVE"` from the backups, or in the app) and
start Thunderbird. v1 was never changed. Anything v2 sent is in the audit log
(`chamberlain doctor --check audit`) and in the Sent folder.

## What the import carries

- **Your own instructions** (the old `instructions` in the profile and in `customisations.yaml`),
  **standing facts** and **keyword watches** go to `config/owner-instructions.yaml`, word for word,
  and the agent is given them at the right moment (see `docs/configuration.md`). Only instruction
  topics you actually changed are carried: the old harness's own shipped texts, which describe
  the old commands, are recognised from its `.state/defaults.json` and left out. If that file is
  missing, every topic is carried and the report says so. A carried topic whose text names an
  old-only command (`briefing`, `monitor`, `outlook status`, `activation complete`,
  `help <topic>`, `schedule sync`) is listed in the report as `instructions_to_review`. It is
  flagged, never deleted. Keyword watches are no longer matched automatically; they become a note
  the agent reads when new mail is shown.
- **Card rules.** A card's own instructions (`customisations.instructions`) and its decisions
  become the new card's `rules`, decisions as "Decided: ..." lines. If that is over the 600
  character limit the card is flagged and due for review. On an existing card, rules are filled in
  only while empty. Other per-card settings (review days, templates, permission defaults) are not
  carried; the report counts them.
- **Important appointments.** An old action marked important (the flag, `-important-` in its id or
  a next step starting "IMPORTANT:") arrives with the important flag, due at the appointment's
  start, reminder from the old reminder time. A repeat import also flags ones imported earlier,
  unless you or the agent changed them since.
- **Read-state.** Your old `filing_changes_read_state` and `reporting_marks_read` become the
  profile options in `read_state`. A standing "mark read" permission per mailbox is proposed; it
  does nothing until you click it in the viewer.
- **Not carried: the weekly sign-in notice roundup.** Such notices are ordinary mail, and a source
  whose own sign-in has failed is raised at once as a sign-in problem instead. The report names
  the dropped line of your old report template.
- `config/report-template.yaml` (your report layout) is created from the old report template if
  absent, never overwritten. A line of the old template that needs a value the new harness has no
  way to fill is left out and named in the import report.
- `config/connections.yaml` is created if absent, never overwritten.
- Google and iCloud secrets are copied into the operating system credential store. The old files
  stay where they are. An existing secret is never replaced.
- **iCloud.** The saved app-specific password is found whichever way the old harness kept it (the
  Mac file, or the Windows encrypted file in the old data folder or the older
  `%LOCALAPPDATA%\Codex\OutlookAutomation`). `migrate prepare` checks it against iCloud and adds
  every iCloud calendar, enabled: the main one ("Calendar" or "Home") as `calendar:icloud`, the
  others as `calendar:icloud-<name>`. Switch one off with `enabled: false`. If iCloud cannot be
  reached, or an earlier import left it off, the calendar stays off and the report names the one
  command: `chamberlain setup icloud register --apple-id EMAIL`.
- **Write journals and approvals.** Every unfinished old write journal is closed as left with the
  old harness (`abandoned`, counted in the report); nothing is resent. Old one-off approvals are
  inert (their hash cannot match a new intent): ask the agent to propose the write again if it is
  still wanted.
