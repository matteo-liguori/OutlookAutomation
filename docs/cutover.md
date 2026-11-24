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
holds the old private guidance text). Running the import again changes nothing.

- `config/connections.yaml` is created if absent, never overwritten. Run
  `chamberlain config validate`.
- Google and iCloud secrets are copied into the operating system credential store. The old
  files stay where they are. An existing secret is never replaced.
- iCloud arrives as a disabled CalDAV source: run
  `python -m chamberlain.connectors.caldav.setup register` to find the calendar collection,
  then enable it.
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
4. Turn the listed private guidance overrides into rule patches by hand.
5. Cards over a size cap are flagged and due for review; trim them at their next update.

## Switch

1. Stop and disable every scheduled task of the old harness.
2. Point your scheduler at `chamberlain tick` and your agent at `chamberlain` (CLI or MCP).
3. Leave the old data root untouched. Delete it only when you no longer want the fallback.

## If something is wrong

Stop Chamberlain, go back to the old harness (it was never changed), and fix the new data
root or start a fresh one. Anything Chamberlain already sent is in the audit log
(`doctor --check audit`) and in your Sent folder.
