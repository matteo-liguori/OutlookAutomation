# Mail clean-up

Old mail you no longer need goes to Trash by rules you set, so mailboxes do not grow forever.
Nothing is ever erased by the harness: Trash keeps a message until it is emptied (Gmail empties
it after 30 days; Exchange keeps Deleted Items until you empty it).

## Rules

In `connections.yaml`:

```yaml
cleanup:
  rules:
  - name: Routine filed mail after a year
    sources: [mail:kcl]
    folder: Codex Triage/Newsletters and Routine FYI
    older_than_days: 365
  - name: Inbox-only mail after three years
    sources: [mail:kcl, mail:personal, mail:shared]
    folder: inbox
    older_than_days: 1096
    only_unfiled: true
  per_run: 500
```

- `folder` is `inbox`, or a folder path as Thunderbird shows it.
- `only_unfiled` keeps a message that is also in one of your own folders (a Gmail label) or
  starred. On Gmail, moving a message to Trash removes every label, so without this a message
  would also vanish from that label.
- `sources: ['*']` means every enabled mailbox.

## Running it

```
chamberlain cleanup plan     # count what each rule would move; changes nothing
chamberlain cleanup grant    # propose the permission; confirm it once in the viewer
chamberlain cleanup run      # move what the rules name to Trash (at most per_run per mailbox)
```

Each move is an ordinary harness write on the "Mail clean-up" card, covered by one standing
permission per mailbox: move to Trash only, from exactly the rules' folders, at most 500 a
day. Only you can confirm it. A large first backlog takes more than one day.
