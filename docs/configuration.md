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
  calendar:icloud:
    kind: caldav
    url: https://caldav.icloud.com/.../calendars/home/
    username: you@icloud.com
    credential: icloud           # name in the credential store
    address: you@icloud.com      # optional; the address invitations are sent from and answered as.
                                 # Needed only when the username is not an email address.
                                 # Without any address the calendar sends no invitations.

write_policy: {}                 # optional overrides of the grant rules (G-12a defaults)
budgets: {}                      # optional storage budgets (M-27 defaults)

schedules:                       # scheduled passes; applied at every start
  - name: morning
    kind: mail                   # mail or calendar
    at: "09:00"
    zone: Europe/London          # optional, defaults to `timezone`
    weekdays: [1, 2, 3, 4, 5]    # 1 = Monday ... 7 = Sunday; default every day
    enabled: true
    presync_minutes: 5           # sync mail folders this long before the pass (default 5, 0 = off);
                                 # keep it at least as long as the time between two ticks

mail_sync:                       # how fresh Thunderbird's folders must be when mail is read
  fresh_minutes: 10              # a folder last synced longer ago is synced again (default 10)
  time_limit_seconds: 300        # all syncing in one call gives up after this long and goes on (default 300)

authorised_senders:              # who may instruct by email (see docs/permissions.md)
  owner: [you@example.com]       # a verified email from these counts like chat
  others:
    - address: assistant@example.com
      scope: [calendar.create]   # intent kinds, or ask_only
      note: my PA
  # trusted_authserv_ids: [mx.google.com, "*.outlook.com"]   # whose Authentication-Results to believe

updates:                         # daily check for a newer release tag (reads tag names only)
  enabled: false
  remote: origin

review:                          # the harness watches how the agent works outside it (docs/review.md)
  enabled: true                  # default true; false stops the transcript scan and the reviews
  new_patterns: 5                # a review is raised once this many new workarounds were logged,
  min_days: 3                    #   but not within this many days of the last review,
  max_days: 60                   #   and after this many days if anything new was logged at all
```

`chamberlain import <old data root>` writes this file from an old installation's profile
the first time and never overwrites it afterwards. The old `schedule.morning` and each
`schedule.continuations` time (`HH:MM` or minutes after midnight) become daily `mail` passes, and
`updates.enabled` and `updates.remote` carry over; other old settings are listed as unmapped in
the import report. Before the scan reads a mail source, and when an agent starts, the harness asks
Thunderbird to sync every folder the scan reads and waits (`mail_sync`). A folder that will not
sync within the time limit is named in the pass report ("Partially verified"), raised as a
`source.stale` event, and tried again at the next tick. The scheduler side is described in
[scheduling](scheduling.md); cutover in [cutover](cutover.md).

## `live_test`: what `chamberlain live-test` may touch

Off by default. When `enabled` is true the command may create clearly marked mail and events
on the sources listed here, and only these. Details and cleanup rules: `docs/live-test.md`.

```yaml
live_test:
  enabled: true
  mail:                              # mailboxes a test may use (each must be a configured source)
    mail:work:
      address: you@example.com       # this mailbox's own address; tests send ONLY to owned addresses
      scratch_folder: Archive        # optional folder id or name for the file-and-back test
    mail:home:
      address: you@example.org
  pairs:                             # cross-account sends between owned mailboxes
    - {source: mail:work, to: mail:home}
    - {source: mail:home, to: mail:work}
  calendars:                         # writable calendars a test may use
    calendar:primary:
      invitee_mail: mail:home        # optional: the owned mailbox the invitation scenarios invite
      invitee_calendar: calendar:home  # optional: that account's calendar (answers the invitation)
    calendar:home: {}
  approve_calendar_edits: false      # true: the runner records per-operation evidence citing this
                                     # setting, so calendar update and delete tests run unattended
  arrival_wait_seconds: 120          # how long to wait for a sent message to arrive
```

Unknown keys are rejected. A `mail` entry for a Thunderbird source must carry that source's
own mailbox address. `pairs`, `invitee_mail` and `invitee_calendar` may only name sources
listed here.
