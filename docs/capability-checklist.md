# Capability preservation checklist

Use this checklist after each rewrite. Entries map the capabilities in section 1 of the architect's plan to regression tests, targeted live scenarios and walkthroughs. Check off a promise for the step under review when the named checks pass. Where coverage is partial, the entry identifies the remaining proof.

The automated capability pins below name their tests. A passing pin verifies the named behavior. Broader live and walkthrough checks stay open until exercised.

The permanent v1 importer, agent-use review, update check, local calendar events and fake provider stay. The owner approved removing the Claude file guard, demo mode and viewer settle buttons.

## Decisions for final review

| Decision | Current choice | Where to revisit and how to verify |
| --- | --- | --- |
| Owl calendar freshness | On 2026-11-26 the owner accepted a successful private create, read-back, delete and absence check as ordinary Owl freshness. | Thunderbird calendar adapter, behind CalendarPort.health. |
| Repeating a move or trash | A completed filing operation may be requested again. This allows trash after restore. Repeating the same operation handle still dispatches once. | Write repeat policy and tests for restore followed by trash. |
| Standing send limits | Retain an owner's explicit per-day limit on a particular grant. The existing hourly send ceiling also applies. | StandingScope.max_per_day and grant coverage tests. |


## Step 0 verification, 2026-11-25

- [x] C2/C3 batch filing and recoverable trash. The Thunderbird rig checks mixed read flags, merged copies and recovery of moved handles after a lost dispatch reply. Live run `20261125T184613-e780` passed `mail.batch_file`, `mail.move_back`, `mail.trash_restore` and `mail.read_state` on `mail:test`. The harness reported zero items left behind.
- [x] C4 expired proposals. Late confirmation and scheduled maintenance both persist the lapse and cancel the waiting operation in the grant lifecycle test.
- [x] C1/C13 attachment indicators. The add-on test queries 55 headers with mixed attachment flags and verifies the result without opening messages. C9/C12 clean-up errors are exercised at the CLI entry point.
- [x] Local development gate passed with 1,022 tests passing and 20 skipped. The remaining checklist is available for the later rewrite steps.

## C1 — Agent surface

Regression tests include `tests/schema/test_registry.py::test_t1_standing_prompt_is_small_and_names_the_three_rules`, `tests/schema/test_registry.py::test_t2_there_are_exactly_twelve_tools`, `tests/writes/test_audit.py::test_g23_every_tool_call_writes_one_tool_call_row`, `tests/attention/test_scenarios.py::test_t7_start_returns_at_most_five_items_and_counts_the_rest`, `tests/memory/test_retrieval.py::test_t8_find_never_returns_a_body_and_hit_lines_are_single_lines`.

- [ ] Twelve tools, prompt <400 characters, closed envelope, error `fix`, tick per call: `tests/schema/test_registry.py`, `tests/schema/test_envelope.py`, `tests/schema/test_errors.py`, `tests/surface/test_surfaces.py`; confirm prompt and tick assertions individually.
- [ ] `start` interactive/scheduled, resume, scheduled day, source capabilities, at most five work items, housekeeping: `tests/attention/test_scenarios.py`, `tests/attention/test_sessions.py`, `tests/cleanup/test_cleanup.py::test_an_agents_start_runs_the_clean_up_and_reports_it`.
- [ ] `next` handled, older mail and paging; `guide` index/one rule: `tests/attention/test_older_mail.py`, `tests/attention/test_handlers.py`, `tests/guidance/test_selection.py`.
- [ ] `find` all scopes, filters, 90-day mail default and no bodies; `open` all parts, paging, leases, attachment text and series/occurrences: `tests/memory/test_retrieval.py::test_t8_mail_find_applies_every_filter_and_pages_the_filtered_hits` checks source, folder role, both flag values, query, inclusive dates, limit and cursor binding. The same file covers the seven scopes, 90-day default and `within` for mail, cards and artifacts. Attachment text and recurrence are exercised by `tests/memory/test_attachment_text.py` and `tests/calendars/test_google_calendar.py`.
- [ ] `keep` all kinds and origins, instruction-email evidence; `forget` release/revoke/cancel; `card` lifecycle, actions, versions, caps and complex reason: `tests/memory/test_handlers.py`, `tests/memory/test_cards.py`, `tests/integration/test_instruction_email.py`, `tests/viewer/test_complex.py`.
- [ ] `propose` intent/scope/evidence/redo, preview and requirements; `do` including report; `reconcile` including happened: `tests/writes/test_handlers.py`, `tests/writes/test_outside.py`, `tests/writes/test_uncertain_send.py`.
- [x] `doctor` six separately selectable check groups and sixteen metrics. `tests/attention/test_doctor_contract.py::test_t16_doctor_runs_six_check_groups_and_returns_sixteen_metrics` checks the wired tool. `tests/attention/test_doctor_contract.py::test_t16_doctor_access_reports_the_private_data_root_without_writing` tests the read-only access check for a readable root, a checkout path and missing or invalid paths. `tests/integration/test_wiring.py::test_t1_doctor_metrics_has_every_key_of_the_targets_contract` checks every metric key.

## C2 — Write kinds

Regression tests include `tests/thunderbird/test_mail.py::test_w24_mark_read_reports_partial_results_and_can_mark_unread`, `tests/writes/test_invitations.py::test_w33_the_invitation_reaches_the_invitees_calendar_and_the_answer_comes_back`, `tests/thunderbird/test_contract.py::test_w29_batch_reconcile_repoints_each_moved_handle`.

- [ ] Send/reply/attachments/importance, draft save/update/discard, move with mark-read, recoverable trash, mark-read up to 200: `tests/writes/test_engine.py`, `tests/thunderbird/test_mail.py`, `tests/contract/test_fake_contracts.py`; live `mail.self_send`, `mail.reply_thread`, `mail.attachment`, `mail.draft_flow`, `mail.read_state`, `mail.move_back`, `mail.batch_file`, `mail.trash_restore`.
- [ ] Calendar create including series/invitations, update/delete scoped occurrence/series and etag, respond: `tests/writes/test_recurring.py`, `tests/writes/test_invitations.py`, `tests/calendars/test_google_invitations.py`, `tests/calendars/test_caldav_invitations.py`; live `calendar.create_update_delete`, `calendar.recurring`, `calendar.invite_flow`, `calendar.recurring_invite`.
- [ ] `outside.action`: `tests/writes/test_outside.py`.
- [ ] Bulk move/trash: one write, one client call, one check, `messages[]` only, including one item. Existing `tests/thunderbird/test_mail.py` and live `mail.batch_file` are leads. Exact call-count and target `messages[]` shape still need proof. Add-on checks `src/chamberlain/connectors/thunderbird/addon/test/bridge.test.mjs` test `P-14 batch filing restores both read states with two bulk updates and skips duplicates` and `W-23 batch filing verifies an explicitly requested read flag` cover batch read state. Batch reconcile repoint has an automated check: `tests/thunderbird/test_contract.py::test_w29_batch_reconcile_repoints_each_moved_handle`.

## C3 — Write safety

Regression tests include the locator checks in `tests/support/mail_contract.py`, `tests/thunderbird/test_mail.py::test_r7_batch_trash_keeps_the_recovery_flag_and_reports_a_merge`, `tests/writes/test_audit.py::test_g20_every_row_chains_to_the_previous_one_and_the_chain_verifies`, `tests/writes/test_uncertain_send.py::test_missing_everywhere_is_reported_once_and_the_agent_may_send_it_again`.

- [ ] State machine, journal before dispatch, one dispatch and crash leases: `tests/writes/test_transitions.py`, `tests/writes/test_properties.py`, `tests/writes/test_engine.py`.
- [ ] Immediate settle, 24-hour recheck, `send.missing`, linked resend, supersede and abandon: `tests/writes/test_uncertain_send.py`, `tests/writes/test_unsettled_writes.py`; live `mail.fault_lost_response`.
- [ ] Duplicate block W-27 and queued Outbox: `tests/writes/test_rulings.py`, `tests/writes/test_uncertain_send.py`; live `mail.outbox_queued`, `mail.repeat_do`.
- [ ] New locators after moves, `merged_existing_copy`, `read_state_preserved`: `tests/support/mail_contract.py`, `tests/thunderbird/test_mail.py`, `tests/writes/test_engine.py`. Batch repoint is checked by `tests/thunderbird/test_contract.py::test_w29_batch_reconcile_repoints_each_moved_handle`. The add-on batch read-state checks are named in C2.
- [ ] 30-day compaction and 730-day hash-chained audit: `tests/writes/test_audit.py`, `tests/writes/test_engine.py`; verify retention boundaries.

## C4 — Grants and permissions

Regression tests include `tests/writes/test_grants.py::test_g8_an_unanswered_proposal_lapses_in_seven_days_and_its_operation_is_cancelled`, `tests/writes/test_grant_rules.py::test_g8_an_unreviewed_grant_is_suspended_fourteen_days_after_review_is_due`, `tests/writes/test_authorise.py::test_g16_standing_daily_limit`, `tests/viewer/test_gate.py::test_g17_a_wrong_origin_host_peer_or_fetch_site_is_403`.

- [ ] Seven-day content-bound one-off; standing scope addresses/domains/folders, multiple sources, daily limit, monthly review, 14-day suspension, one-year expiry and card binding: `tests/writes/test_grants.py`, `tests/writes/test_grant_rules.py`, `tests/writes/test_authorise.py`. Lapse persistence and cancellation are checked by `tests/writes/test_grants.py::test_g8_an_unanswered_proposal_lapses_in_seven_days_and_its_operation_is_cancelled`. An explicitly configured daily limit is retained.
- [ ] One permission table and generated documentation: `tests/writes/test_permission_table.py`.
- [ ] Known recipients and seed; authenticated authorised senders: `tests/writes/test_recipients.py`, `tests/domain/test_senders.py`.
- [x] 200 sends/hour per source, with all-owner recipients exempt. `tests/writes/test_authorise.py::test_g16_the_200_send_boundary_exempts_only_all_owner_recipients` tests the 199/200 boundary and mixtures in To, Cc and Bcc. `tests/writes/test_grants.py::test_g16_the_hourly_send_limit_refuses_the_next_send` exercises dispatch, source isolation and recovery as the window expires using a lower configured limit.
- [ ] `read_state` via standing grant: `tests/writes/test_read_state.py`, `tests/runs/test_read_state.py`. Viewer confirmation loopback/token/origin: `tests/viewer/test_gate.py`, `tests/viewer/test_confirm.py`.

## C5 — Attention

Regression tests include `tests/attention/test_watermark.py::test_a_nothing_is_skipped_and_nothing_is_shown_twice_across_folders`, `tests/attention/test_scenarios.py::test_a26_two_events_that_overlap_are_raised_with_their_rule_and_clear_when_apart`, `tests/guidance/test_selection.py::test_selection_once_per_always_repeats_and_3_related_checks`.

- [ ] All event kinds: `mail.new_unmatched`, `action.due`, `reminder.due`, `card.review_due`, `grant.needed`, `grant.review_due`, `operation.uncertain`, `send.missing`, `source.stale`, `source.sign_in_needed`, `calendar.conflict`, `user.note`, `storage.over_budget`, `storage.orphans`, `update.available`, `schedule.pass_due`, `report.due`, `harness.review`: `tests/attention/test_scenarios.py`, `tests/attention/test_tick.py`, `tests/runs/test_notes_updates.py`, `tests/review/test_review.py`. `tests/domain/test_events.py::test_a1_the_18_event_kinds_have_exact_public_names` checks all 18 public spellings and aliases. Audit each kind's dedupe, expiry and re-raise separately.
- [ ] Pull, piggyback and wake exit 10: `tests/attention/test_handlers.py`, `tests/viewer/test_server.py::test_tick_prints_the_passes_a_wake_adapter_should_start`; verify exit code assertion.
- [ ] 48 guidance rules, selection/budgets/once-per, private overrides and owner instruction topics: `tests/guidance/test_rules.py::test_a12_exactly_48_guidance_rules_are_shipped`, `tests/guidance/test_selection.py`, `tests/integration/test_budgets.py`; live `harness.guidance_budgets`.
- [ ] Watermark all folders except Trash/Junk/Drafts, older-mail question and card suggestions: `tests/attention/test_watermark.py`, `tests/attention/test_older_mail.py`; live `mail.watermark_suggestion`.

## C6 — Memory

Regression tests include `tests/memory/test_sweeper.py::test_m3_other_tracks_compactors_run_inside_the_one_sweep`, `tests/memory/test_sweeper.py::test_m24_a_backup_never_holds_bytes_and_a_restore_cannot_revive_expired_content`, `tests/memory/test_evidence_refs.py::test_r4_repoint_keeps_the_handle_across_a_move`.

- [ ] Cards/actions, caps and per-card complex approval: `tests/memory/test_cards.py`, `tests/viewer/test_complex.py`.
- [ ] Imported context, notes/evidence/artifacts, owners/expiry/leases, refs/handles and repoint: `tests/importer/test_context.py`, `tests/memory/test_artifacts.py`, `tests/memory/test_evidence_refs.py`, `tests/memory/test_retrieval.py`.
- [ ] Single sweeper, orphans/quarantine, budgets, backup/restore and history: `tests/memory/test_sweeper.py`, `tests/integration/test_budgets.py`, `tests/memory/test_history.py`; check explicit backup/restore and single-sweeper proof.
- [ ] Calendar labels and review findings: `tests/viewer/test_front_page.py`, `tests/review/test_review.py`; confirm label persistence.

## C7 — Runs and report

Regression tests include `tests/runs/test_runs.py::test_a_scheduled_session_that_ends_without_a_report_leaves_an_incomplete_run`, `tests/runs/test_mail_sync.py::test_a_scheduled_pass_is_synced_ahead_and_finds_fresh_folders`, `tests/runs/test_read_state.py::test_reported_mail_is_marked_read_only_with_the_option_and_the_owners_grant`.

- [ ] Config schedules, pre-sync, missed/incomplete runs and stale-day guard: `tests/runs/test_runs.py`, `tests/runs/test_mail_sync.py`, `tests/runs/test_passes_not_held_back.py`.
- [ ] Harness report sections including access, appointments, grant reviews and 'Partially verified'; agent outline, kept report, private template and outside actions: `tests/runs/test_runs.py`, `tests/importer/test_report_template.py`, `tests/writes/test_outside.py`; audit each section explicitly.
- [ ] Mark-read only after kept report: `tests/runs/test_read_state.py`.

## C8 — Viewer

Regression tests include `tests/viewer/test_front_page.py::test_a_scheduled_pass_ends_with_the_owners_report_as_the_front_page`, `tests/viewer/test_front_page.py::test_categories_local_events_and_the_owners_acknowledgement`, `tests/viewer/test_confirm.py::test_g19_decline_revoke_and_keep_are_audited_as_the_human`.

- [ ] Reports on the calendar front page and earlier reports: `tests/viewer/test_front_page.py`, `tests/viewer/test_pages.py`.
- [ ] Grant confirm/keep/revoke and complex approve/decline/take-back: `tests/viewer/test_confirm.py`, `tests/viewer/test_complex.py`.
- [ ] Timeline shows uncertain writes as read-only history. `tests/viewer/test_views.py::test_w41_uncertain_history_is_read_only_and_outside_needs_you` covers the viewer surface and refusal of the removed endpoint. Agent and harness settlement remain C3.
- [ ] Calendar categories, local events, important appointments and hold-to-acknowledge. `tests/viewer/test_front_page.py` checks the authenticated acknowledgement route. `tests/viewer/test_static_scripts.py::test_viewer_scripts_parse_and_acknowledgement_requires_a_continuous_hold` runs the served JavaScript with deterministic pointer, keyboard, visibility and animation-frame events.
- [ ] Cards, Timeline, Today/activity, Runs/reports, Live-test runs, Storage and owner notes: `tests/viewer/test_pages.py`, `tests/viewer/test_views.py`, `tests/viewer/test_live_test_page.py`, `tests/runs/test_notes_updates.py`; full viewer walkthrough required.

## C9 — Mail clean-up

Regression tests include `tests/cleanup/test_cleanup.py::test_the_background_clean_up_is_off_by_default_and_runs_once_a_day`, `tests/cleanup/test_cleanup.py::test_no_limit_but_a_big_run_asks_the_agent_to_glance_at_trash`, `tests/surface/test_surfaces.py::test_s16_cleanup_reports_malformed_profile`.

- [ ] Off by default, one-year filed and three-year qualifying Inbox, Trash only, `cleanup plan|grant|run`, automatic daily `start`, report line, no daily cap and >100 flag: `tests/cleanup/test_cleanup.py`; CLI error handling has checks in `tests/surface/test_surfaces.py::test_s16_cleanup_reports_malformed_profile` and `tests/cleanup/test_cleanup.py::test_old_inbox_mail_goes_to_trash_only_after_the_owner_confirms`. Verify explicit age boundaries, bulk call count and grant lifecycle.

## C10 — Export

Regression tests include `tests/export/test_thread_export.py::test_a_thread_exports_once_per_message_in_order_with_people_and_tagged_files`, `tests/export/test_thread_export.py::test_quotes_can_be_kept_and_nothing_found_is_an_error`, `tests/memory/test_sweeper.py::test_exports_go_after_a_week`.

- [ ] Thread IDs/handles/`--file`, `--keep-quotes`, zip and manifest, temporary location swept after seven days, never on a card: `tests/export/test_thread_export.py`; Seven-day expiry is checked by `tests/memory/test_sweeper.py::test_exports_go_after_a_week`; explicit handle and no-card proofs need checking.

## C11 — Live test

Regression tests include `tests/livetest/test_run.py::test_the_whole_suite_runs_green_and_leaves_only_the_trash`, `tests/livetest/test_run.py::test_dry_run_only_and_skip_choose_scenarios_without_touching_anything`, `tests/livetest/test_run.py::test_the_lost_response_scenario_needs_a_hook_that_fires`.

- [ ] Commands grant/run (`--only`, `--skip`, `--keep`, `--dry-run`, `--retry`, `--approve-calendar-edits`, `--json`), sweep/report: `tests/livetest/test_cli.py`, `tests/livetest/test_run.py`, `tests/livetest/test_cleanup.py`; verify every option.
- [ ] Mail scenarios: `mail.self_send`, `mail.cross_send`, `mail.reply_thread`, `mail.draft_flow`, `mail.attachment`, `mail.read_state`, `mail.move_back`, `mail.batch_file`, `mail.trash_restore`, `mail.find_by_message_id`, `mail.repeat_do`, `mail.refuse_non_owned`, `mail.fault_lost_response`, `mail.watermark_suggestion`, `mail.outbox_queued`.
- [ ] Calendar scenarios: `calendar.create_update_delete`, `calendar.all_day`, `calendar.timezone` (the plan calls its function `timezone_case`), `calendar.recurring`, `calendar.invite_flow`, `calendar.recurring_invite`; harness `harness.guidance_budgets`, `harness.doctor_clean`.
- [ ] Marker, parallel runs, fake-world self-test, viewer page: `tests/livetest/test_markers_and_report.py`, `tests/livetest/test_run.py`, `tests/viewer/test_live_test_page.py`; assert bulk and parallel sends/checks and API-handled dialogs. Scenarios are identifiers for future live validation, not evidence of a successful live run.

## C12 — CLI

Regression tests include `tests/schema/test_registry.py::test_t2_there_are_exactly_twelve_tools`, `tests/surface/test_mcp_stdio.py::test_the_server_over_a_real_stdio_process_on_a_minimal_profile`, `tests/livetest/test_cli.py::test_it_is_off_until_switched_on_and_a_dry_run_changes_nothing`, `tests/viewer/test_server.py::test_tick_prints_the_passes_a_wake_adapter_should_start`.

- [ ] Twelve tools plus `tools`, `schema`, `prompt`, `config validate|show`, `cleanup`, `export`, `addon update [--restart]`, `import`, `recipients seed`, `live-test`, `tick`, `viewer`, Thunderbird/Google/CalDAV setup and `dev check|fix`: `tests/surface/test_surfaces.py`, `tests/livetest/test_cli.py`, `tests/thunderbird/test_setup.py`, `tests/thunderbird/test_update.py`, `tests/calendars/test_google_setup.py`, `tests/calendars/test_caldav_setup.py`; audit each command. `demo init` and `guard` are explicitly dropped.

## C13 — Connectors

Regression tests include `tests/contract/test_fake_contracts.py::test_the_fakes_satisfy_the_port_protocols`, `tests/thunderbird/test_contract.py::test_p1_the_connectors_satisfy_the_port_protocols`, `tests/thunderbird/test_mail.py::test_r8_a_thunderbird_prompt_during_a_send_is_answered_and_reported`.

- [ ] Thunderbird mail folders/search/read/chunked attachments/sync/all writes and API dialog handling: `tests/thunderbird/test_mail.py`, `tests/thunderbird/test_bridge.py`, `tests/thunderbird/test_addon.py`; live mail scenarios C11. Ensure the full mail contract suite runs against both fake and full-stack rig. Add-on test `P-3 P-4 query reports attachment flags in batches from one header scan` checks attachment flags.
- [ ] Thunderbird contacts: `tests/thunderbird/test_contacts.py`. Owl calendar experiment without invitations: `tests/calendars/test_owl_calendar.py`, `tests/thunderbird/test_owl_calendar_addon.py`.
- [ ] Google OAuth/invitations/recurrence and CalDAV/iCloud invitations/recurrence/`commitments: false`: `tests/calendars/test_google_contract.py`, `tests/calendars/test_caldav_contract.py`, `tests/calendars/test_google_invitations.py`, `tests/calendars/test_caldav_invitations.py`; live C11 calendar scenarios on applicable backends. Owl cannot invite.
- [ ] Fake provider for self-test, native host and fault hook: `tests/contract/test_fake_contracts.py`, `tests/thunderbird/test_fault_hook.py`, `tests/livetest/test_run.py`.

## C14 — Sync and freshness

Regression tests include `tests/runs/test_mail_sync.py::test_folders_synced_too_long_ago_are_synced_again_when_the_agent_starts`, `tests/runs/test_mail_sync.py::test_a_folder_that_will_not_sync_is_reported_waited_for_once_and_retried`, `tests/attention/test_scenarios.py::test_e3_incomplete_coverage_brings_the_coverage_rule`.

- [ ] Sync before read, ten-minute freshness, five-minute limit, stuck folders reported and retried, and coverage gaps. `tests/attention/test_sync_before_read.py::test_a24_a_stale_mail_source_is_synced_before_normal_tool_reads` observes sync completion before the normal `start`, `find` and `open` read paths and checks the fresh-cache case. `tests/attention/test_sync_before_read.py::test_a25_a_stuck_folder_is_reported_while_other_folders_are_read` checks a failed folder alongside a successful read and verifies that the failed folder is reported in `next`. `tests/memory/test_retrieval.py::test_e3_a_stale_source_is_incomplete_not_empty_and_1_related_checks` checks disclosure in reply coverage. Scheduled pre-sync, overall budget and catch-up remain covered by `tests/runs/test_mail_sync.py`, `tests/runs/test_sync_overall_limit.py` and `tests/runs/test_stuck_folder_older_mail.py`.

## C15 — Review

Regression tests include `tests/review/test_review.py::test_the_log_keeps_tool_names_and_short_summaries_only`, `tests/review/test_review.py::test_a_review_round_reaches_the_agent_and_only_worrying_reaches_the_owner`.

- [ ] Transcript improvisation log, review rounds, worrying findings, simplified onto shared code: `tests/review/test_review.py::test_the_log_keeps_tool_names_and_short_summaries_only`, `tests/review/test_review.py::test_a_review_round_reaches_the_agent_and_only_worrying_reaches_the_owner`. Guard test is obsolete and must be removed with guard.

## C16 — Permanent v1 import

Regression tests include `tests/importer/test_import.py::test_s14_a_second_import_changes_nothing`, `tests/importer/test_import.py::test_s17_the_old_windows_icloud_password_moves_and_its_calendars_connect`, `tests/importer/test_report_template.py::test_the_old_report_template_becomes_a_private_layout`.

- [ ] Profile, cards/context, grants/authority, journals, DPAPI/iCloud credentials, report template, owner instructions and recipients: `tests/importer/test_import.py`, `tests/importer/test_context.py`, `tests/importer/test_carry_over.py`, `tests/importer/test_report_template.py`; audit each source, especially owner instructions and recipients. Retain importer after cutover.

## C17 — Setup, migration and onboarding

- [x] The pull-only `setup.agent` guide, persistent selection, done/next/waiting state and provider handoffs are exercised in `tests/attention/test_handlers.py`, `tests/connections/test_onboarding.py` and the individual provider setup suites. The isolated walkthrough built the native host and XPI, used a simulated installation on loopback, bound a synthetic source, and verified a scheduled report in the viewer. Live first-use validation uses the owner's existing installation.
- [x] The public capability listing, tagged release notes, config migration preview/apply and recoverable backups are exercised in `tests/surface/test_surfaces.py`, `tests/surface/test_config_updates.py`, `tests/runs/test_release_notes.py` and `tests/connections/test_migrate_config.py`.
- [x] Resumable v1 detection, backup, import, comparison and switching are covered in `tests/importer/test_migration_cli.py` and `tests/importer/test_migrate_flow.py`. A copy of the active v1 installation matched 171 cards and 40 actions with zero local differences. The rehearsal used an isolated schedule manifest. Automated scheduler tests cover task identity, rollback and interrupted switching.

## Additional inventory

- [ ] MCP stdio: `tests/surface/test_mcp_stdio.py`. Update check: `tests/runs/test_notes_updates.py`. Data-root guard: `tests/test_dataroot.py`. Demo-specific tests must be replaced where they are the sole proof of retained behavior.

## Stage 4 verification

The local gate passed 1,070 tests with 20 skips. The installed Thunderbird add-on is 3.0.4. The two harness scenarios passed, and the targeted test-mailbox Message-ID lookup and cross-account watermark suggestion passed after Thunderbird restarted. The mail-only scheduled pass stored its report on a disposable profile using the real test mailbox. An authenticated headless Edge session opened all ten viewer pages, and all fifteen API reads returned HTTP 200. See live runs 20261126T014932-9255 and 20261126T015115-df44.

Owl freshness is settled by the owner on 2026-11-26, including KCL. The owner also confirmed that a fresh authorized move or trash after restoration proceeds within 24 hours without another confirmation. Restoring an item alone requests no action.
