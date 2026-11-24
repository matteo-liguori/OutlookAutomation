# Configuration: `config/connections.yaml`

One private file in the data root tells the harness which mailboxes and calendars exist,
how writes are governed and when scheduled passes run. It is validated on every start
(`chamberlain config validate`); `chamberlain config show` prints it without secrets.
Unknown keys are rejected, so a pasted password fails validation: secrets live only in
the operating system's credential store, and this file names them.

```yaml
version: 1
timezone: Europe/London          # the day used by schedules and the wrong-day stop
bridge_dir: bridge               # Thunderbird native-host files (never swept)

sources:                         # id prefix gives the role: mail:, calendar:, contacts:
  mail:work:
    kind: thunderbird
    account_id: account1
    inbox_folder_id: account1://INBOX
    # sent, drafts and trash folder ids as written by `chamberlain import` or setup
  calendar:primary:
    kind: google                 # or caldav, owl
    account: you@example.com
    calendar_id: primary

write_policy: {}                 # optional overrides of the grant rules (G-12a defaults)
budgets: {}                      # optional storage budgets (M-27 defaults)

schedules:                       # scheduled passes; applied at every start
  - name: morning
    kind: mail                   # mail or calendar
    at: "09:00"
    zone: Europe/London          # optional, defaults to `timezone`
    weekdays: [1, 2, 3, 4, 5]    # 1 = Monday ... 7 = Sunday; default every day
    enabled: true

updates:                         # daily check for a newer release tag (reads tag names only)
  enabled: false
  remote: origin
```

`chamberlain import <old data root>` writes this file from an old installation's profile
the first time and never overwrites it afterwards. The old `schedule.morning` and each
`schedule.continuations` time (`HH:MM` or minutes after midnight) become daily `mail` passes, and
`updates.enabled` and `updates.remote` carry over; other old settings are listed as unmapped in
the import report. The scheduler side is described in
[scheduling](scheduling.md); cutover in [cutover](cutover.md).
