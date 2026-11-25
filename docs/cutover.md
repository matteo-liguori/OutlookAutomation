# Cutover checklist: old harness to Chamberlain

The old data root is the fallback until you delete it. Nothing here modifies it, and at no
point may both systems write.

## Before

1. Close the old harness and any scheduled task that runs it. Copy the whole old data
   folder (for example `OutlookAutomation-data`) to a new place. **Import the copy.**
   The importer refuses a database with an unmerged `-wal` file: that means the old
   harness is still open or crashed.
2. Choose a new, empty data root for Chamberlain. It must not be inside the old one.
3. Install Chamberlain and note that the Thunderbird add-on must be reinstalled once
   (`python -m chamberlain.connectors.thunderbird.setup`).

## Import

```
chamberlain --data <new data root> import <copy of old data root>
```

Read the printed summary, then `<new data root>/import/report.json` (private; it also
holds nothing private). Running the import again is safe. It adds only what is missing and
never overwrites a change made since, in v2, by you or the agent: card context ("context from the
old harness"), card rules, important-appointment flags, your private instructions, the report
layout and the read-state options. What it added is in the new report; the earlier report is kept
beside it as `report.1.json`, `report.2.json` and so on.

What comes across, and how:

- **Your own instructions** (the old `instructions` in the profile and in `customisations.yaml`),
  **standing facts** and **keyword watches** go to `config/owner-instructions.yaml`, word for word,
  and the agent is given them at the right moment (see `docs/configuration.md`). Only instruction
  topics you actually changed are carried: the old harness's own shipped texts (which describe the
  old commands) are recognised from its `.state/defaults.json` and left out. If that file is
  missing, every topic is carried and the report says so; delete the ones that only describe old
  commands. Keyword watches are not matched automatically any more; they become a note the agent
  reads when new mail is shown.
- **Card rules.** A card's own instructions (`customisations.instructions`) and its decisions
  become the new card's `rules`, decisions as "Decided: ..." lines. If that is over the 600
  character limit the card is flagged and due for review. On an existing card, rules are filled in
  only while empty. Other per-card settings (review days, templates, permission defaults) are not
  carried; the report counts them.
- **Important appointments.** An old action marked important (the flag, `-important-` in its id or
  a next step starting "IMPORTANT:") arrives with the important flag, due at the appointment's start,
  reminder from the old reminder time. A repeat import also flags ones imported earlier, unless you
  or the agent changed them since.
- **Read-state.** Your old `filing_changes_read_state` and `reporting_marks_read` become the
  profile options in `read_state`. A standing "mark read" permission per mailbox is proposed; it
  does nothing until you click it in the viewer.
- **Not carried: the weekly sign-in notice roundup.** The old harness collected sign-in notices
  (new device and similar messages) into a weekly list. Chamberlain does not: such notices are
  ordinary mail, and a source whose own sign-in has failed is raised at once as a sign-in problem
  instead. The report names the dropped line of your old report template.

- `config/report-template.yaml` (your report layout) is created from the old report template
  if absent, never overwritten. A line of the old template that needs a value the new harness
  has no way to fill (the weekly roundup) is left out and named in the import report.
- `config/connections.yaml` is created if absent, never overwritten. Run
  `chamberlain config validate`.
- Google and iCloud secrets are copied into the operating system credential store. The old
  files stay where they are. An existing secret is never replaced.
- iCloud: the saved app-specific password is found whichever way the old harness kept it
  (the Mac file, or the Windows encrypted file in the old data folder or the older
  `%LOCALAPPDATA%\Codex\OutlookAutomation`). The import checks it against iCloud and adds every
  iCloud calendar, enabled: the main one ("Calendar" or "Home") as `calendar:icloud`, the
  others as `calendar:icloud-<name>`. Switch one off with `enabled: false`. If iCloud cannot be
  reached, the report says so and the calendar stays off.
- Then, or if you imported before this existed, one step does it:
  `python -m chamberlain.connectors.caldav.setup import-old --old-data <old data folder>`.
- Sources the old profile bound to Outlook COM or native Outlook, or that lack a folder id,
  are listed as skipped. Bind them in Thunderbird and add them to the profile by hand.

## Parallel read-only running

For a few days run Chamberlain for reading only. Do not confirm grants and do not let the
agent send, file or write calendar entries. Compare its briefing with the old one. The old
harness keeps doing all the writing, so exactly one system writes.

## Before the first write

1. Open the viewer. Every standing grant is **suspended** and due for review. It has no
   scope (the old scope was free text), so it authorises nothing. Recreate each one you
   still want as a new standing grant from the old wording shown in its evidence, and
   confirm it.
2. Every unfinished old write journal is an **uncertain** operation. Look in Sent, the
   folders and the calendar, then tell the viewer whether each one happened. An imported
   operation is never retried and the agent cannot reconcile it.
3. Old one-off approvals are inert (their hash cannot match a new intent). Ask the agent to
   propose the write again if you still want it. No recipient is known yet.
4. Read `config/owner-instructions.yaml` (your own rules, carried from the old harness) and
   delete any topic that only describes the old commands.
5. Cards over a size cap are flagged and due for review; trim them at their next update.

## Switch

1. Stop and disable every scheduled task of the old harness.
2. Point your scheduler at `chamberlain tick` and your agent at `chamberlain` (CLI or MCP).
3. Leave the old data root untouched. Delete it only when you no longer want the fallback.

## If something is wrong

Stop Chamberlain, go back to the old harness (it was never changed), and fix the new data
root or start a fresh one. Anything Chamberlain already sent is in the audit log
(`doctor --check audit`) and in your Sent folder.
