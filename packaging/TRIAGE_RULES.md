# Monitoring pass

Keep instructions brief and positive; state the desired behaviour, give each rule
one home and link to it elsewhere.

1. Read the complete briefing and local profile. `run start` gives collection and
   filing bounds. Use the profile's enabled sources as required coverage.
2. Check `Test-OutlookSession.ps1 -CheckCom`, then complete
   `Start-OutlookSendReceive.ps1`. Require observed SyncStart/SyncEnd, success and
   no errors before collection. Follow MAIL_HELPERS.md for recovery.
3. For each configured mail store, run `Test-OutlookMailboxFreshness.ps1` within
   15 minutes of collection. Collect with `Collect-OutlookMonitor.ps1`, following
   all pages. Keep explicit gaps for unavailable or stale sources.
4. Read each new candidate's full body, including meeting messages. Recognise
   tracked work, retrieve relevant threads, reconcile cards/actions and classify
   the result. Use THREAD_CARD_UPKEEP.md for triggered reviews.
5. Apply the user's approved routine filing preferences within `filing_windows`,
   retaining consequential or uncertain messages. Verify each destination and
   preserved read state, then record coverage with `filing record`.
6. Read each enabled calendar for today plus three Europe/London days. Work
   Exchange uses Read-OutlookMonitorCalendar.ps1; optional Google uses
   `calendar google-prepare` and Collect-GoogleMonitor.js through the user's
   connected tools; optional iCloud uses Invoke-iCloudCalDav.ps1.
7. Follow MONITOR_REPORT_TEMPLATE.md and paged report data. Record actual handoff,
   gates and source receipts; inspect status, finalize, and reflect. Describe
   incomplete required coverage as partially verified.

Before suggesting or booking times, check all enabled calendars for conflicts,
travel, adjacent commitments and all-day events. Verify authorised writes and
duplicates with readback. Keep reply clocks anchored to actual sends or responses.
Report due reminders and unsnoozed needs-user actions; keep unchanged waiting/watch
work quiet unless its schedule requires attention.

Agree a schedule with the user after the first supervised pass. The harness
supplies records and reminders when run; a scheduler or assistant runs each pass.
