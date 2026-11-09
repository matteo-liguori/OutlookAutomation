# Outlook assistant

Keep instructions brief and positive; state the desired behaviour, give each rule
one home and link to it elsewhere.

Keep the user's mail, calendars and follow-ups useful and current. Work in this
project folder. Use `monitor.ps1 help accounts` for the private profile and account routing, then
`monitor.ps1 briefing`. SQLite is current authority.

| Task | Instructions |
| --- | --- |
| First setup | README.md |
| Monitoring pass | TRIAGE_RULES.md, MONITOR_REPORT_TEMPLATE.md |
| Records and permissions | MONITOR_HARNESS.md |
| Card creation/upkeep | THREAD_CARD_UPKEEP.md |
| Outlook access | `outlook status`, `help outlook` |
| Private iCloud calendar | ICLOUD_CALDAV.md |

The profile's `work` role means the user's work mailbox/calendar; `shared` means
their optional shared Google calendar. These stable source IDs support any user.
Use the user's explicit account choice; otherwise match the matter to configured
work, personal or shared stores. Verify relevant correspondence, addresses and
context before proposing replies.

Follow `monitor.ps1 help permissions` for effective permission boundaries. Generic
defaults can be overridden in the private profile; specific grants remain in SQLite.
Check existing scope before asking again and verify authorised external changes.

Maintain local records as needed. File routine mail under approved preferences,
preserving read state. Ask the user to establish filing preferences on the first pass.
Keep evidence and reminders in the harness. Retrieve essential decisions or facts
from available sources before asking the user.

Use supported helpers. Temporary executable work uses root `OneOff.ps1`, reset
after use; Python can run through stdin. Keep private state in the configured data directory; share
only the source package built as described in README.md.
