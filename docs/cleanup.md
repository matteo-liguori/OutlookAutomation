# Mail clean-up

Old mail you no longer need goes to Trash by rules you set, so mailboxes do not grow forever.
It is **off by default**. Nothing is ever erased by the harness: Trash keeps a message until it
is emptied (Gmail empties it after 30 days; Exchange keeps Deleted Items until you empty it).

## Rules

In `connections.yaml`:

```yaml
cleanup:
  enabled: true        # run by itself once a day; default false
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
```

- `folder` is `inbox`, or a folder path as Thunderbird shows it.
- `only_unfiled` keeps a message that is also in one of your own folders (a Gmail label) or
  starred. On Gmail, moving a message to Trash removes every label, so without this a message
  would also vanish from that label.
- `sources: ['*']` means every enabled mailbox.
- There is no limit on how many move. When more than 100 leave one mailbox in a run, the
  report line asks the agent to glance at Trash there, in case a rule caught too much.

## How it runs

With `enabled: true`, the harness runs the rules by itself, at most once a day, when an agent
calls `start` to get ready for its report. The agent does nothing for it: `start` returns a
`housekeeping` line such as "Clean-up moved 12 emails to Trash: Inbox-only mail after three
years (mail:kcl) 12." for the report. It is silent when nothing moved.

The first time, it proposes one permission per mailbox: move to Trash only, from exactly the
rules' folders. Confirm each once in the viewer's Needs you page; until then nothing moves and
the line says what is waiting. Every move is an ordinary harness write on the "Mail clean-up"
card, audited like any other. Steady state is a few messages a day; a large first backlog is
better moved once by hand (below), so no `start` has to wait for it.

By hand, whether or not it is enabled:

```
chamberlain cleanup plan     # count what each rule would move; changes nothing
chamberlain cleanup grant    # propose the permissions
chamberlain cleanup run      # move what the rules name to Trash now
```

Pass `--source mail:NAME` to `cleanup plan`, `cleanup grant` or `cleanup run` to apply the configured rules to one mailbox.
