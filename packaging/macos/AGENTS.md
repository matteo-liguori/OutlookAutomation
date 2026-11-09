# Outlook assistant: Mac pilot

Keep instructions brief and positive; state the desired behaviour, give each rule
one home and link to it elsewhere.

Work in this folder. Start with README.md for the native Mac pilot and capability
checks. Once initialised, use `.venv/bin/python -m monitor briefing`; retrieve
account/platform details with `help accounts` and `outlook status`. SQLite is current authority.

| Task | Instructions |
| --- | --- |
| Native Mac setup, mailbox access and pilot | README.md |
| Current records and permissions | MONITOR_HARNESS.md |
| Card creation/upkeep | THREAD_CARD_UPKEEP.md |
| Report structure | MONITOR_REPORT_TEMPLATE.md |

Match reads to the configured user's verified account, especially on borrowed
computers. Use `python -m monitor outlook <operation>` and `help outlook`. The private
profile selects Outlook or Thunderbird independently for each source and records
its verified bindings. See `outlook_platforms/thunderbird/README.md` for Thunderbird setup.
Verify the account, source
coverage and relevant full messages before reconciling tasks. State partial
coverage clearly. Keep private records in the configured data directory and use thread caches for mail.

Sending needs conversation approval or an applicable harness permission covering
sender, recipients, subject, wording and attachments. Calendar writes and routine
filing need appropriate authority. Preserve read state during read-only tests.
Complete authorised work, verify outcomes, and ask for essential missing decisions.
Store reminders and permissions in the harness. The first pilot establishes
working access; agree any recurring schedule with the user afterwards.
