"""Operation-specific instructions, loaded only by relevant actions or triggers."""

RELEVANCE_REVIEW = (
    'At review, judge ongoing relevance from the date, context and risk of error. '
    'Close unless a concrete current reason warrants tracking; technical incompleteness alone does not. '
    'Use judgment in clear cases. Ask if genuinely unsure or missing a material fact you cannot reasonably retrieve. '
    'Record why; email evidence can be retrieved again.'
)

GUIDES = {
    'reporting': '# Reporting\n\nUse MONITOR_REPORT_TEMPLATE.md for report structure. Personal reporting preferences belong in private profile instructions.reporting; they do not change permissions or required coverage.',
    'card-input': '`case edit --input -`: id, expected_revision, idempotency_key, optional title (180 chars), purpose (600 chars/three lines) and note (2000 chars). Edits preserve action/review clocks. Pending notes appear in briefing and case show until handled. `case note-ack --input -`: id, expected_revision, idempotency_key, note_ids and outcome (600 chars). Handle notes and record resulting changes before acknowledging; leave blocked notes pending. Existing send/calendar permission boundaries still apply.',
    'ui': 'Optional current-work viewer: install requirements-ui.lock in the project environment, then run ui. Opens a local card grid and action timeline; click for details, label edits or a note for the next agent pass. Notes stay pending until explicitly handled; help card-input. Browsing and label edits preserve task/review clocks. No Outlook scans. Ctrl+C stops it; --no-browser prints the private local link.',
    'outlook': 'Outlook access: `outlook <operation> --input -` takes JSON. Enabled operations and fields are listed below.\nUse configured source IDs and ISO timestamps with offsets. The harness handles account binding and backend sequencing.\n`collect` prepares/checks the source and returns a bounded page; optional run_id imports all pages into that run.\n`send` and draft operations use approval_id; omitted message fields come from the exact SQLite approval.\nMaterialise applicable conversation/standing scope without asking again. Submission is not verified delivery.\nPreserve read state and inspect completeness/errors. Use help permissions/calendar-decisions for authority.\nWhere listed, trash uses an exact one-off scope: operation trash-message, mailbox, source_folder_id, entry_id, destination_folder_id (the configured Trash), internet_message_id. It is recoverable. move-reconcile/trash-reconcile take the original approval_id and check outcomes without repeating the mutation.\nCalendar writes use an exact scope: operation calendar-create/calendar-update/calendar-delete, calendar_id, event_id; create/update require icalendar; update/delete require expected_icalendar from a current full calendar-read. Invitation handling depends on adapter capability.\nInstallation internals are available only when diagnosing with `outlook status --details`.',
    'case-organisation': '`case merge/split --input -` requires expected_revisions, idempotency_key, reason and evidence_ids. Merge: target_id and source_ids. Split: source_id, new target_id, action_ids and target title/position. Preserve action IDs, dependencies, clocks and permissions; reconcile context after organisation. This does not complete obligations.',
    'scheduling': '`schedule plan` supplies future London/UTC instants; create the applicable app schedule and read it back, then use schedule verify. Use its bounded UTC DTSTART/RRULE or individual one_shot_rules; optional triggered_at checks a recorded firing time. Same-date continuations target the current mail chat. Schedule only authorised future work and stop it when no useful work remains.',
    'accounts': 'Account identities, source roles and personal routing come from the private profile. Respect explicit account choices and applicable permissions.',
    'briefing': 'briefing has four sections: open-thread recognition/goals, due actions/alerts, periodic card reviews, commands. Use review next for due reviews; briefing --case <id> or --message-input - for related work, case show <id> for the full card, help <operation> for procedures. Follow paging cursors. Future/snoozed work and full card bodies stay out of the default briefing.',
    'context': '`context put --input -` takes id, entity, text and evidence_ids (plus relevant provenance). Keep one current canonical record for the subject; update it rather than making parallel notes. Retrieve only matching records with context get <term>. Use the structured stdin bridge shown by help records.',
    'action-review': '`action review --input -` takes action_id, parent expected_revision, idempotency_key, review_kind (records/mail/source-check), outcome, evidence_ids and review_policy. Policy requires urgency immediate/high/normal/low, interval_days1–90 (maximum3 for immediate/high), basis calendar/working, reason and evidence_ids. Describe the actual review scope. A review does not complete work, authorise external action or reset existing follow-up anchors.',
    'evidence': '`evidence put --input -` takes compact body, source, kind, relevant timestamps and provenance/locators; id is optional. Evidence is immutable. Store full email bodies with thread put (case_id, message), not evidence put. Use the structured stdin bridge shown by help records.',
    'mail-coverage': 'Current mailbox access is verified, but an older coverage gap remains. Collect and reconcile the missing interval using run start bounds and the recorded coverage_gap_from/through; preserve the gap until verified recovery. Disclose any unfinished catch-up. This gap alone does not require another delivery test.',
    'records': r"""## Commands and input

Discover current work with `briefing`; retrieve `case show/search`, `context get`,
`evidence show`, `thread messages/find`, `health`, `approval list/show` explicitly.
Retrieve full bodies separately. Match conversation/reference/store identifiers.

Global `--format json`, `--limit`, `--cursor`, `--db` precede commands. Follow every
cursor with the same query, reassemble `json_chunk` parts, restart changed queries.
Pages allow 12,000 characters. Interpreter override: `monitor.ps1 -Python <path>`.

Structured writes/collector results go directly through stdin:

```powershell
. .\OutlookHarness.Common.ps1
Invoke-OutlookHarness -Arguments @('evidence','put','--input','-') -InputObject $payload
```

Use this bridge for structured input.""",
    'card-writes': r"""## Card writes

One current card contains context, position, review/report markers and evidence
index; its displayed actions are canonical harness rows. Apply upkeep on due work,
related evidence or explicit requests. Recognition entries are not review triggers.

Commands below take `--input -`:

| Command | Payload |
| --- | --- |
| `evidence put` | Compact body, source, timestamps, provenance, exact locators; optional id |
| `thread put` | case_id, message (full email) |
| `case update` | id, expected_revision, idempotency_key, evidence_ids, case, complete actions; optional replacement decisions |
| `action review` | action_id, parent expected_revision, idempotency_key, review_kind (records/mail/source-check), outcome, evidence_ids, review_policy |

Case: title, purpose, context, position, status open/closed; optional aliases/match_keys.
match_keys is an object with sources/conversation_ids/references lists of nonempty strings.
Action: id, owner, status needs-user/waiting/watch/done/dismissed, next_step,
completion_condition, reopening_condition, follow_up; optional deadline, promised_at,
explicit_review, snooze_until, reviewed_at/review_outcome. Dates use offsets; unknown
historic dates remain unknown. Non-email tasks use user-instruction evidence.

Use normal word spacing in all prose. Shorten wording rather than joining words.
Limits: purpose 600 characters/three lines; context 6,000 ceiling; position 1,200;
next_step 400; completion/reopening conditions 300. Condense context, not obligations.
Resolution needs completion_reason/completion_evidence, including user dismissal.
Reconcile cross-thread dependencies before retiring prerequisites. Saves atomically
remove validated done/dismissed actions and terminal one-off approvals; preserve
unresolved siblings/pending wording. Reopening needs evidence/reason and fresh IDs/cache.
Decisions: id, text, evidence_ids, optional same-thread action_ids; replace spent
notes (`[]` clears, omission preserves). Scoped notes expire with their final action.

For action timing use `monitor.ps1 help action-scheduling`.""",
    'permissions': r"""## Permissions

Reusable scope lives in standing_approvals, one-offs in approvals.
Logging or reading a thread message surfaces thread-bound and global candidate
standing sending grants with their exact limits. Check the full message and
attachments against the grant; carry out qualifying authorised sends without
asking again. Repeated notices require duplicate checks, not repeated sends.
`approval put`: kind, id, optional case_id, actual granted_at, evidence_ids, exact message/task scope;
standing adds title/summary/triggers/boundaries. Keep permission scope in these rows.

Resurface monthly on the original London grant anniversary, clamped for short months;
unknown dates need review now. Only explicit user confirmation renews review;
reading/use/silence does not renew or revoke unchanged authority. `approval review`:
id, expected_revision, decision keep/revoke/spent, reason/evidence_ids. Unknown dates
get a truthful reconfirmation anchor. `approval flag` uses id, expected_revision,
reason/evidence_ids and suspended=true for unclear scope. Retire delivered one-offs;
other permissions end on withdrawal, expiry or proven scope end. Spent standing
needs scope_ended=true; closure only ends it with
end_on_case_close. Preserve pending approved wording until reconciled.""",
    'pass-receipts': r"""## Pass receipts

`run start [--activated]` returns ID, mail bounds and per-store filing_windows.
`run ingest <run>`: source a configured mail source, started_at, ordered
pages, freshness; identities/window/probe must match. Follow `run candidates` pages.
`run record` accepts one record or an atomic array:

| kind | Fields |
| --- | --- |
| candidate | id, source, entry_id, evidence_ids, disposition, review_reason; material: body_evidence_id and case_id/case_revision or no_update_reason |
| source | source a configured calendar source, status, complete, four days, events, evidence_ids, provenance |
| gate | name refresh/records/title, evidence_ids; refresh: success/completed_at/provenance; records: success; title: value/readback evidence |
| handoff | delivered, case_ids/case_revisions, action_ids, event_ids/cancellation_ids (`source:id`), roundup_ids, issue_ids, approval_ids, four days |

For scoped quiet decisions, handoff accepts quiet_cases with case_id, revision,
reason and evidence_ids. These acknowledge review, not delivery; required work
cannot be suppressed. Report data exposes quiet_allowed.

Weekly roundups appear only for Sunday runs starting 08:30-10:00 London (allowing
early/late 09:00 startup); incomplete or ineligible handoffs preserve the queue.

Events: stable id/title, offset-qualified start/end, location, all_day.
Use explicit_cancellations for authoritative cancelled records; preserve active replacements. Mail receipts
come from ingest. Follow `report data`, check `run status`, then `run finalize`;
incomplete gates/sources remain partial. Acknowledge only actual delivered verified
coverage; undelivered work/cancellations remain pending. Finalization records metrics;
the assistant interprets mail and the harness validates records.""",
    'storage': r"""## Automatic storage policy

Relevant mail is in thread_messages; attachments in
`<data-root>/monitor/threads/<case-id>/attachments/` (safe path from `thread path`).
Each thread owns its copies, deduplicated by message ID/hash within that thread.
`Get-OutlookMessage.ps1 -CaseId` reuses/downloads mail and attachments up to 10 MiB;
-ForceRefresh reads live, -DownloadLargeAttachments fetches needed larger files.
`thread messages/find` retrieves metadata/bodies; attachment-path/attachment allocates/registers.
Routine unrelated mail is not persistently cached; portal outcomes need date/reference.

Closure deletes bodies/assets, keeping compact outcome/source IDs for 365 days,
then expires thread-specific records/evidence. Preserve expiry dates on reads and
start fresh on reopening. Retain useful open caches and unresolved approvals;
standing preferences remain until changed.

Health/checkpoints/calendar baselines remain current in place, without access logs
or expiry. Preserve gaps until verified recovery. Transfer/verify unresolved pass
work into pending_work before retiring superseded finalized runs. Inactive finalized
payloads/collector snapshots expire after 24 hours; preserve current receipt/running
work. Deduplication keeps the seven-day filing window plus active/pending references;
owned temporary files last seven days.

Startup/closure/finalization run cleanup and retry visible failures. Maintenance
status/clean --dry-run/clean/threads expose it; unknown ownership needs reconciliation.
Keep two daily, two weekly, at most one version/schema backup. First write per London
day/ISO week creates its slot; verify replacements before rotation. Keep backups
within these slots. Backups exclude disposable caches/expired records,
preserving obligations/approvals/checkpoints and the matching private profile JSON.
Recover the profile with `python manage_data.py --restore-profile-from <backup.db>
--profile-output <new-profile.json>`; existing files are preserved.
Restore current format to a new path
with writers closed, doctor/compare before switching; never copy a live WAL alone.

For code changes run Test-Harness.ps1 and relevant offline fixtures;
live sends/calendar writes/restarts are not regression tests.""",
    'mail-access': 'Check current profile and source health; follow any applicable delivery-test permission before escalating access uncertainty. Use the authorised account and route. Report remaining gaps promptly; the user handles credentials and sign-in.',
    'calendar-decisions': r"""## Calendar decisions

Use the enabled calendars from the private profile. Before offering/accepting/declining
times, check all enabled sources for travel, adjacent/all-day commitments, transitions and
aftercare. Respect exclusive all-day ends; disclose conflicts/unavailable sources.
Establish availability from complete calendar coverage.

Check consequential commitments/deadlines learned from mail for calendar presence.
Report the existing event or exact needed entry. Where stored authority covers a
clear email commitment, verify details/target/duplicates/conflicts, add, read back
and report. Obtain authority for changes to conflicting events separately.

Medical/user-marked important appointments need separate seven/two/one-day needs-user
acknowledgements, snoozed to each milestone. Repeat until acknowledged; generic
acknowledgement clears the current milestone only. Newly learned appointments get
meaningful remaining milestones; urgent ones surface immediately. Stop at start,
cancellation or dismissal.""",
    'activation': r"""## Activation

The first full `briefing` starts setup automatically; try title/follow-up setup now;
no user reply is required to try. On a user reply first use `briefing --user-active`
and resolve pending setup before other message content. If an actual startup attempt
fails, record `activation defer --input -` (reason, evidence_ids), continue the mail
pass and retry when the user replies; no repeated background attempts.
Briefing reads local chat-title and automation metadata and clears verified work
automatically, or names only missing work. Unknown metadata is not proof of absence.
Runtime activity is not evidence that the user is present.
Database-backed calls repeat a short reminder until `activation complete --input -`:
title_readback, evidence_ids, followup {status ACTIVE, target_thread_id, rrule}.
If no same-day times remain use followup {status none-needed}. Record actual app
readbacks; the helper verifies the London date, chat target and remaining instants.
If the user declines follow-ups, record status declined and decline_evidence_ids.

09:00 Europe/London daily. Read London time, set/read back exactly
`Mail check DD/MM/YYYY`; plan/create/verify one heartbeat for still-future same-date
14:00/19:00 only. Read back rules and replan after delays; no next-day substitutes.
Late activation requires catch-up from the completed watermark even if continuations
are declined.""",
    'card-upkeep': '# Thread review\n\n' + RELEVANCE_REVIEW + r"""

Read the current card and relevant evidence. Keep purpose recognisable and context
as the current brief. Reconcile actions, dependencies and spent permissions; retain
genuine obligations with their IDs, deadlines, promises, snoozes and chase anchors.
Record retirement accurately without claiming an unverified outcome happened.
Save one replacement with revision/idempotency guards; use help card-writes for fields.

`review next` supplies one due card; instructions appear only after over two hours
without review activity (or on first use). Follow paging cursors.
Keep: `review complete --input -` takes id, expected_revision, idempotency_key,
outcome (why tracking remains useful), evidence_ids and optional interval_days (1–90,
default 7). Close: use case update with resolved/dismissed actions and reasons.
Then call review next again until complete. Unfinished cards remain due.
After action-related whole-card work, use review complete or action review to reset
the periodic clock. Reading alone does not count. Reply clocks and snoozes stay intact.
""",
    'filing': 'Use filing status for bounded windows and the private profile for approved destination folders. Establish filing preferences before changing mail. Preserve read state; no deletion, spam reports or unsubscribing. Record verified counts, locators and failures with filing record.',
    'thread-cache': r"""## Thread cache

`thread put --input -` takes case_id and message (the full email with metadata/body).

Relevant mail is in thread_messages; attachments in
`<data-root>/monitor/threads/<case-id>/attachments/` (safe path from `thread path`).
Each thread owns its copies, deduplicated by message ID/hash within that thread.
`Get-OutlookMessage.ps1 -CaseId` reuses/downloads mail and attachments up to 10 MiB;
-ForceRefresh reads live, -DownloadLargeAttachments fetches needed larger files.
`thread messages/find` retrieves metadata/bodies; attachment-path/attachment allocates/registers.
Routine unrelated mail is not persistently cached; portal outcomes need date/reference.""",
    'retention': r"""Closure deletes bodies/assets, keeping compact outcome/source IDs for 365 days,
then expires thread-specific records/evidence. Preserve expiry dates on reads and
start fresh on reopening. Retain useful open caches and unresolved approvals;
standing preferences remain until changed.

Health/checkpoints/calendar baselines remain current in place, without access logs
or expiry. Preserve gaps until verified recovery. Transfer/verify unresolved pass
work into pending_work before retiring superseded finalized runs. Inactive finalized
payloads/collector snapshots expire after 24 hours; preserve current receipt/running
work. Deduplication keeps the seven-day filing window plus active/pending references;
owned temporary files last seven days.

Startup/closure/finalization run cleanup and retry visible failures. Maintenance
status/clean --dry-run/clean/threads expose it; unknown ownership needs reconciliation.
Keep two daily, two weekly, at most one version/schema backup. First write per London
day/ISO week creates its slot; verify replacements before rotation. Keep backups
within these slots. Backups exclude disposable caches/expired records,
preserving obligations/approvals/checkpoints. Restore current format to a new path
with writers closed, doctor/compare before switching; never copy a live WAL alone.

For code changes run Test-Harness.ps1 and relevant offline fixtures;
live sends/calendar writes/restarts are not regression tests.""",
}

GUIDES['action-review'] += '\n\n' + RELEVANCE_REVIEW

def guide(topic):
    if topic not in GUIDES:
        raise ValueError("Unknown guidance topic")
    from .profile import PROFILE
    instruction = PROFILE.get('instructions', {}).get(topic, GUIDES[topic])
    result = {"topic": topic, "instruction": instruction}
    if topic in {'permissions','calendar-decisions','filing'}:
        from .policy import effective
        result['effective_permission_defaults'] = effective(PROFILE)
        result['permission_source'] = 'Individual grants and approval scope are in SQLite; profile overrides affect default boundaries only.'
    if topic == 'accounts':
        result['profile'] = {k:PROFILE[k] for k in ('name','timezone','mail','calendars','roles','calendar_providers','paths') if k in PROFILE}
        from automation_paths import data_root
        result['data_root'] = str(data_root())
    if topic == 'outlook':
        from outlook_platforms.api import status
        result.update(status())
    return result


COMMAND_TOPICS = {
    'case edit': 'card-input', 'case note-ack': 'card-input',
    'briefing': 'briefing',
    'case': 'card-upkeep', 'review': 'card-upkeep',
    'outlook': 'outlook',
    'case update': 'card-writes', 'case merge': 'case-organisation',
    'case split': 'case-organisation', 'action review': 'action-review',
    'approval': 'permissions', 'run': 'pass-receipts', 'report': 'pass-receipts',
    'filing': 'filing', 'schedule': 'scheduling', 'thread': 'thread-cache',
    'maintenance': 'retention', 'backup': 'retention', 'calendar': 'calendar-decisions',
    'refresh': 'mail-access', 'health': 'mail-access',
    'evidence': 'evidence', 'context': 'context',
}


def help_for(words):
    if not words:
        return {'usage': 'monitor.ps1 help <topic or command>',
                'topics': [{'topic': k, 'summary': v.splitlines()[0].lstrip('# ')}
                           for k, v in sorted(GUIDES.items())],
                'examples': ['help case update', 'help action review', 'help filing']}
    if words[0]=='outlook' and len(words)>1:
        from outlook_platforms.api import available_operations
        if words[1] not in available_operations():raise ValueError('Unknown Outlook operation; use help outlook')
    target = ' '.join(words)
    topic = target if target in GUIDES else COMMAND_TOPICS.get(target)
    if topic is None:
        topic = COMMAND_TOPICS.get(words[0])
    if topic is None:
        raise ValueError('Unknown instruction topic or command; use monitor.ps1 help')
    return guide(topic)


def context_prompts(items):
    prompts = []
    gaps = [i for i in items if i.get('type') == 'source_health'
            and i.get('source', '').startswith('mail:') and i.get('status') == 'partial'
            and i.get('coverage_gap_from')]
    if gaps:
        prompts.append(dict(type='mail_coverage_prompt', **guide('mail-coverage')))
    if any(i not in gaps and i.get('type') == 'source_health'
           and (i.get('source', '').startswith('mail:') or i.get('source') == 'outlook:refresh')
           and i.get('status') != 'current' for i in items):
        prompts.append(dict(type='mail_access_prompt', **guide('mail-access')))
    if any(i.get('type') == 'approval_review' and i.get('review_due') for i in items):
        prompts.append(dict(type='permission_review_prompt', **guide('permissions')))
    if any(any(key in i for key in ('backup_rotation_warning', 'thread_cache_warning', 'maintenance_warning'))
           for i in items):
        prompts.append(dict(type='maintenance_prompt', **guide('retention')))
    return prompts
