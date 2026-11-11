# Mail helpers

Keep instructions brief and positive; state the desired behaviour, give each rule
one home and link to it elsewhere.

Use `monitor.ps1 outlook <operation>` (Mac: `python -m monitor outlook`).
`outlook status` lists capabilities/fields; `help outlook` gives the workflow.
For Mac Thunderbird, use the [command correspondence and setup](../outlook_platforms/thunderbird/README.md).
The helpers below are Windows backend/compatibility references. Scope: AGENTS.md.

| Need | Helper |
| --- | --- |
| Accounts/stores | Inspect-Outlook.ps1 -Action Accounts/Stores |
| Folders/recent/search | Read-OutlookMail.ps1, explicit Store/Folder/Limit/MaxScan |
| Full tracked mail | Get-OutlookMessage.ps1 -EntryId -StoreId -CaseId -Output Json |
| Body/recipient search | Search-OutlookMailDetails.ps1 |
| Contacts | Search-OutlookContacts.ps1 |
| Bounded collection | Collect-OutlookMonitor.ps1; follow SnapshotId/NextOffset |

For approved sends, verify the sender account and reuse a successful refresh
within 15 minutes, otherwise refresh once. Preflight exact recipients, subject
and distinctive body with Test-OutlookSendDuplicate.ps1 across Sent/Outbox/Drafts.
Reconcile matches and incomplete scans before submission. Submit approved items
back-to-back through Send-OutlookStoreMailSafe.ps1 -BatchSubmitOnly with explicit
Store/To/Cc/Bcc/Body/Attachments. Refresh once, then independently verify unique
Sent copies, recipients, subject, normalised body and attachment count. Record
each item's sent, skipped, unsent or uncertain result in the harness.

Outbox disappearance alone leaves delivery uncertain. Keep sender, recipients,
body and attachments fixed during recovery and reconcile transport before retrying.
Meeting invitations need their calendar item and meeting-message classes checked
as well as ordinary mail. Verify calendar writes through independent readback.

For COM faults, Test-OutlookSession.ps1 -CheckCom identifies the session. A concrete
COM failure can use Restart-ClassicOutlook.ps1 in the current logged-in user session;
retry once after verified restart. Continue independent sources and report gaps.
Credential and account setup stays with the user. Recovery helpers inspect exact
items and retain their transport guards; obtain material scope changes explicitly.
