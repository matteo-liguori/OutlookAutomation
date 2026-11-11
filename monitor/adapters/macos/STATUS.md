# Native Mac status

The 9 November 2026 integration rerun used the common `monitor outlook`
interface on Legacy Outlook 16.111, macOS 15.6.1 arm64 and Python 3.12.16.
Reusable fixes stay on `codex/mac-integration-test`; private receipts stay local.

## Passed live

- Verified the configured mailbox in Outlook, then used common account/discovery,
  folder inventory, bounded search, full-message reads, empty search and repeated
  reads. Mail read flags remained unchanged.
- Common help hid move, refresh, contacts and other unavailable operations.
- Stable recent Inbox watermarks; all-page collection/ingestion through the
  common interface with current coverage. The isolated harness ingested 15
  messages, passed doctor, cached a self-message/attachment, hit the cache on a
  repeated read, and removed cached messages on case closure.
- Exact approved self-draft save/update, decoded attachment SHA-256 verification,
  one submission per approval and duplicate suppression. Delivered Inbox/Sent
  copies matched the updated body, recipients, subject and attachment hash.
- Two self-mails delivered without manual Send & Receive. The timed test observed
  unique Inbox and Sent copies within 52 seconds of invocation (including polling
  overhead); submission returned after about 11 seconds. This is observed delivery,
  not a guarantee of automatic transport completion for every send.
- A simulated persisted `submitting` journal state blocked retry. Independent
  Inbox/Sent reconciliation found unique copies; restoring the original
  reconciled journal preserved duplicate suppression. No actual transport crash
  was injected and no general reconciliation command was added.
- This run's self-mail copies were removed recoverably. Reconciled calendar
  cleanup passed delayed public readback; all new test tokens were absent and
  finite test permissions retired. Both harness databases passed doctor.
- Public calendar reads expanded three daily occurrences across spring DST,
  retaining 09:00 London wall time. A 28 March all-day fixture spanned 23 hours.

## Fixes and unresolved findings

- Logical `drafts` reads now use the verified draft-folder binding when Outlook
  exposes duplicate Drafts names. A missing bound folder fails closed.
- Maintenance fixtures now canonicalise the default Mac temporary directory,
  preserving the production symlink/junction refusal. Setup smoke tests include
  maintenance so `/var` versus `/private/var` regressions stay visible.
- Calendar exports now compare nonrecurring subject/start/end with native
  properties; disagreement omits the source and marks coverage incomplete.
- Immediate calendar update/removal is unreliable during synchronization. One
  update returned success but the public read retained its original end time;
  another retest read the updated value consistently. Outlook's generic save
  command failed on the test event. Deleted fixtures can reappear with new native
  IDs after synchronization. Reconcile by the exact test token and verify delayed
  absence; immediate ID disappearance is insufficient. Calendar writes remain
  absent from normal help.
- General Mac filing remains unresolved and hidden. A follow-up reproduced the failure on a
  newly delivered synthetic message: ordinary/global-ID/object-directed moves,
  alternate destination references, native copy, command-side selection and
  synchronize/move/synchronize failed independent source/destination readback.
  The account was not exposed through normal account collections, default
  account or Inbox ownership. Registered App Intents provided no filing action.
  A diagnostic UI move worked, but UI control and Gmail connector backends are
  excluded from the requested command-only Outlook implementation.
- Microsoft [documents AppleScript automation of Google accounts synced through
  Microsoft Cloud as unsupported](https://support.microsoft.com/en-us/support/known-issues/known-issues-syncing-google-accounts-to-the-microsoft-cloud).
  This matches the observed account binding and move failures; it does not prove
  every Outlook account type lacks command-driven filing. Windows filing is unchanged.
- An authorized isolated Legacy Outlook profile using direct Gmail IMAP did
  expose a real native account. An experimental account-ID/Inbox-ID binding
  allowed commands without a selected window or folder. The common public move
  command filed one server-backed incoming self-message, preserved unread state
  and suppressed a repeated approval. Delayed source/destination readback confirmed
  the move and a nonzero destination IMAP UID. Initial synchronization also exposed
  UID-zero local outgoing placeholders; those are not proof of server filing.
- That experiment does not meet the general Mac requirement. IMAP synchronizes
  [mail only, without contacts, calendars, tasks or notes](https://support.microsoft.com/en-us/outlook/sync-basics-what-you-can-and-cannot-sync).
  It is not a replacement account setup for Microsoft 365: Exchange Online has
  [removed Basic authentication, and Outlook does not support OAuth for POP/IMAP](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online).
  Microsoft also documents [Legacy Outlook's Exchange Online retirement starting
  October 2026](https://support.microsoft.com/en-us/outlook/end-of-support-for-legacy-outlook-for-mac).
  That Exchange retirement is distinct from the direct-IMAP test; neither establishes
  a supported general solution for current Outlook or an institutional mailbox.
- The experimental implementation and tests were preserved locally rather than
  enabling filing in the normal harness. All new IMAP test copies were removed
  recoverably; two delayed reconciliations found none in the tested active folders.
  Its five finite test approvals were retired and the isolated database passed
  doctor. The original Outlook profile was retained; the separate test profile
  remains installed and was the last running/default profile. No primary harness
  profile was migrated and no institutional account was added or tested.
- Current Outlook web add-ins are a separate possible integration surface, not a
  verified replacement. The [Office.js mailbox interface](https://learn.microsoft.com/en-us/javascript/api/outlook/office.mailbox)
  does not establish the required general folder-filing command. Microsoft's
  documented [Graph message move](https://learn.microsoft.com/en-us/graph/api/message-move)
  requires mailbox-service authorization, while add-in EWS operations are documented
  for [Exchange on-premises](https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/web-services).
  No UI automation or mailbox-service API was adopted as the filing backend.
- Open-composer refresh, real interrupted
  transport/concurrent-edit recovery, live recurrence exceptions/cancellations,
  non-self sends, complex/large attachments, meeting invitations, secondary
  calendars, New Outlook and Windows runtime remain unverified here.
- Original known-message/appointment UI comparison remains incomplete. Earlier
  test fixtures already present at startup were preserved.

## Offline verification

Under the unmodified default Mac TMPDIR, setup smoke passed 235 tests with one
Windows-only skip. The broader suite completed 344 tests (327 passed, 17 environment skips);
the optional iCloud test module was excluded because the Mac lockfile does not
install `caldav`. Packaging passed five tests with one Windows-only skip.
Synthetic tests never contact Outlook and do not establish full Mac compatibility.

The checkout inherited an ignored manifest from an extracted bundle. That stale
receipt was preserved privately before source-checkout setup; package integrity
checks remain enforced on newly built bundles. Profiles, mail, attachments,
databases, live receipts and credentials are excluded from commits and PRs.
