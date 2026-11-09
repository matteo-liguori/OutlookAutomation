# Native Mac status

Integrated on `main` behind the common harness interface. The shared harness and
Windows helpers remain intact. Use harness help for ordinary operations.

## Passed live

- Bound Legacy Outlook access to the confirmed Gmail account and owned folders.
- Native mail search, exact reads, Message-ID metadata, read-state preservation.
- Calendar recurrence expansion and native private fixtures spanning spring DST;
  all-day 23-hour appointment; create/update/delete and verified cleanup.
- Immutable native collector pages, stable recent Inbox freshness and ingestion
  through the existing SQLite harness; isolated harness doctor passed.
- Exact approved self draft save/display/update and attachment SHA-256 checks.
- Native send submission and duplicate prevention; actual delivery/body/hash
  verified after manually invoking Outlook Send & Receive.
- Native attachment export, isolated case-cache writes/repeated reads and closure.
- All created test mail/calendar objects removed from active sources, finite
  permissions retired. Mail deletion was recoverable. Private receipts stay local.

## Outstanding

1. Native Gmail move: Outlook accepted JXA and AppleScript moves but the synthetic
   message stayed in Inbox. Verification correctly failed and blocked blind retry.
   Resolve native transport support before enabling routine filing.
2. Automatic send flushing: the attachment message stayed in Outbox until the
   Outlook Send & Receive UI action. Establish a supported native sync path and
   delivery verification; a submitted result does not mean delivered.
3. Open composer refresh: native edits were correct, but an already open composer
   showed stale text. Reopened view and delivered mail showed the updated body.
4. Interrupted-operation reconciliation and concurrent-edit recovery need broader
   live coverage. Do not blindly retry ambiguous submissions/moves.
5. New Outlook runtime, Windows runtime, non-self sending, large/complex attachments,
   meeting invitations, secondary calendar binding and other Windows-only helpers
   remain untested or unsupported. Live recurrence exception/cancellation tests
   remain; synthetic cases cover them.
6. Main now canonicalises owned-path aliases during relocation and uses canonical
   paths in the data-separation fixtures. Re-run with default Mac TMPDIR to confirm;
   that native rerun has not been performed on the Windows integration host.
7. Exact original known message/appointment UI comparison remains incomplete.

## Offline verification

The source package manifest and packaging checks were verified at this checkpoint.
The expanded Mac suite has 65 synthetic tests; these never contact live Outlook.
All 202 smoke tests passed with `TMPDIR=/private/tmp`; packaging had five passes
and one Windows-only skip. The default-temp alias failures above remain open. No full Mac
compatibility claim is made from synthetic tests.

Follow `README.md`, `AGENTS.md` and `outlook_platforms/README.md` on resumption.
No recurring automation was created. Do not include private profiles, mail,
attachments, databases, live receipts or credentials in Git.
