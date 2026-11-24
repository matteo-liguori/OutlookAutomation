# 11 Ported test intent

The old harness's safety tests are the specification. This table carries
their intent across, without their code. Paths are under
`C:\dev\OutlookAutomation\tests\` (read-only reference). Each track turns
its rows into named tests; a row is done when a test names the rule ID.

## Grants (old "approvals") - writes track

| Old test | Intent | Rule |
| --- | --- | --- |
| `test_approvals::test_independent_tables_and_reading_never_renews` | Reading or listing a grant never renews it. | G-7 |
| `::test_anniversary_short_months_and_london_dst` | Monthly review on the grant's anniversary, clamped for short months, stable across DST. | G-6 |
| `::test_user_keep_advances_anniversary_without_scope_change` | "Keep" moves the review date only. | G-7, G-3 |
| `::test_unknown_grant_due_now_not_migration_date` | A grant with no known date is due at once. | G-6 |
| `::test_scope_and_timers_cannot_be_changed_by_put` | Scope and timers are immutable. | G-3 |
| `::test_early_material_change_flags_only_this_permission` | A changed grant is a new grant; siblings untouched. | G-3 |
| `::test_retirement_requires_evidence_and_does_not_touch_sibling` | Ending one grant never affects another. | G-2 |
| `::test_case_bound_scope_closes_but_global_scope_survives` | Card-bound grants end with the card; global ones survive. | G-9 |
| `::test_expired_one_off_stays_protected_but_requires_immediate_review` | Expired is a status, visible, never "available". | G-2, G-8 |
| `::test_spent_one_off_cannot_be_reactivated` | Spent is final. | G-4 |
| `::test_non_email_one_off_has_exact_scope_and_same_review_lifecycle` | One lifecycle for every intent kind. | G-1 to G-10 |
| `::test_pending_wording_and_permission_evidence_survive_cleanup_backup` | A grant in use and its evidence survive clean-up. | G-10, M-11 |
| `test_threadcache::test_expired_send_notice_never_reports_available` | An expired grant is never shown as usable. | G-8 |
| `::test_expired_pending_approval_is_preserved_in_recovery_until_reconciled` | Expiry does not release an uncertain operation. | G-10, W-13 |

## Writes (old "journals") - writes track and connectors

| Old test | Intent | Rule |
| --- | --- | --- |
| `test_thunderbird::test_uncertain_send_is_never_reissued` | No re-dispatch, ever. | W-8, W-17 |
| `::test_exact_move_is_durable_and_changed_scope_cannot_reuse_it` | A repeat returns the stored receipt; a changed intent cannot reuse the grant. | W-16, W-18 |
| `::test_uncertain_move_reconciles_without_a_second_mutation` | Reconcile is read-only. | W-8, P-11 |
| `::test_client_busy_before_dispatch_can_be_retried_with_same_grant` | Proven-not-applied releases the grant. | W-10, W-11, P-10 |
| `::test_trash_uses_only_approved_configured_recoverable_destination` | Trash is recoverable and bound. | W-4, P-14 |
| `::test_calendar_write_exact_id_and_old_version` | Calendar writes name the exact event and old version. | P-16 |
| `::test_calendar_timeout_settles_only_from_exact_readback_without_reissuing` | A timeout settles only from a read-back. | W-10, W-12 |
| `::test_calendar_reconcile_settles_a_late_write_without_reissuing_it` | A late write is found by reconcile. | P-11 |
| `::test_calendar_cancellation_remains_authoritative_in_shared_ingestion` | Cancelled is not missing. | P-16 |
| `test_thunderbird_mail::test_send_reconciliation_never_resubmits_or_needs_original_attachment_file` | Reconcile needs only `Prepared` and the key. | P-11 |
| `::test_uncertain_move_refreshes_both_folders_and_never_moves_again` | Refresh exactly the folders involved, then read. | P-11 |
| `::test_draft_update_requires_old_raw_hash_and_exact_bound_folder` | Draft update uses the old hash. | P-9 |
| `::test_draft_save_is_cached_and_exposes_stable_locator_and_raw_hash` | A saved draft returns a stable locator and its hash. | P-6 |
| `::test_attachment_changed_bytes_are_rejected` | Attachments are matched by hash. | W-3, M-7 |
| `::test_attachment_paths_outside_private_data_are_rejected`, `::test_attachment_symlink_escape_is_rejected` | Attachments come only from the artifact store. | W-3, M-8 |
| `test_threadcache::test_pending_send_survives_cleanup` | A pending operation's artifacts survive. | M-11 |

## Read state and coverage - connectors and memory

| Old test | Intent | Rule |
| --- | --- | --- |
| `test_thunderbird::test_filed_mail_is_left_read_only_when_the_owner_allows_it` | A move changes the flag only when the grant allows. | W-23, P-14 |
| `::test_mark_read_needs_the_permission_default_and_reports_partial_results` | Marking read is a write; partial results are reported. | W-23, W-24 |
| `::test_snapshot_continuation_is_immutable_and_source_bound` | Cursors are immutable and bound. | M-17 |
| `::test_persisted_message_locator_resolves_new_session_integer` | Locators survive a restart. | P-6, D-5 |
| `::test_mail_and_calendar_choose_separate_clients_without_fallback` | No fallback between routes. | P-1 |
| `test_thunderbird_mail::test_previews_keep_distinct_same_rfc_physical_copies` | Physical copies stay distinct. | P-7, M-20 |
| `::test_freshness_requires_callback_and_two_recent_stable_samples` | Freshness is vouched for, never guessed. | P-5 |
| `test_reliability::test_stale_wrong_store_and_incomplete_empty_scans_do_not_verify` | Empty and incomplete is not "nothing". | E-3, P-4 |
| `::test_cancelled_source_records_are_reported_as_cancellations` | Cancelled is reported as such. | P-16 |

## Bridge - Thunderbird track

| Old test | Intent | Rule |
| --- | --- | --- |
| `test_thunderbird::test_frames_reject_truncation_and_oversize` | The frame reader rejects short and oversize frames. | bridge contract (track d) |
| `::test_windows_bridge_roundtrips_and_rejects_unauthenticated_clients` | Local clients authenticate. | bridge contract |
| `::test_native_host_disconnects_never_abort_python` | A dropped client never crashes the host. | bridge contract |
| `::test_registration_preserves_other_installation_and_calendar_is_optional` | Registering does not disturb another installation. | bridge contract |
| `test_thunderbird_mail::test_binary_transfer_crosses_multiple_frames_and_hashes_real_bytes` | Chunked transfer, hashed. | P-8 |
| `::test_corrupt_download_leaves_no_partial_or_final_asset` | Corrupt bytes leave nothing behind. | P-8, M-7 |

## Memory - memory track

| Old test | Intent | Rule |
| --- | --- | --- |
| `test_threadcache::test_independent_copies_deduplicate_only_inside_thread_and_close` | Bytes deduplicate; ownership does not. | M-6, M-10 |
| `::test_expiry_is_fixed_and_startup_resumes_it` | Expiry is fixed at creation and resumes after restart. | M-21 |
| `::test_long_open_threads_keep_bodies_and_files` | An open action keeps what it owns. | M-4 |
| `::test_failed_deletion_is_visible_and_retried` | Failed deletes are retried and visible. | M-23 |
| `::test_startup_finishes_closure_interrupted_after_commit` | An interrupted sweep completes next time. | M-21 |
| `::test_backup_excludes_disposable_cache_and_expired_completion`, `::test_startup_and_backup_cannot_revive_expired_completion` | Backups cannot revive what expired. | M-24 |
| `::test_rejects_orphan_body_and_attachment_escape` | No ownerless storage; no path escape. | M-4, M-8 |
| `::test_reopening_starts_empty_and_manual_task_snoozes` | A reopened card starts with no cached content. | M-3 |
| `test_evidence_lifecycle::test_two_threads_have_independent_evidence_not_a_shared_ownership_list` | Evidence referrers are independent. | M-3 |
| `::test_expired_source_cannot_be_reused_as_new_authority` | Expired evidence cannot authorise. | M-15, G-13 |
| `::test_new_nested_card_exports_are_rejected` | Evidence never holds a card dump or mail body. | M-15 |
| `::test_unclaimed_inputs_expire_but_pending_work_survives` | Unowned expires; owned by live work survives. | sweeper properties |
| `test_cards`, `test_card_retirement` (sizes and closure) | Closing compacts; caps hold. | M-12, lifetime table |

## Attention - attention track

| Old test | Intent | Rule |
| --- | --- | --- |
| `test_guidance::test_default_briefing_is_recognition_due_work_and_commands_without_card_bodies` | `start` shows due work and no bodies. | T-7, A-9 |
| `::test_default_problem_alert_points_to_help_without_dumping_procedure` | Over budget becomes a pointer. | selection step 5 |
| `::test_prompt_is_deduplicated_and_preserved_across_briefing_pages` | Once per session and version. | selection step 2 |
| `::test_due_permission_prompt_in_full_and_scoped_briefing_then_disappears_on_review` | `grant.review_due` shows until reviewed, then goes. | A-2, G-8 |
| `::test_snoozed_card_stays_quiet_then_due_and_explicit_case_work_prompts` | Snoozed is silent until due. | A-2 |
| `::test_storage_warning_prompt_is_removed_when_warning_clears` | Events expire when no longer true. | A-2 |
| `::test_access_prompt_tracks_actual_failure_recovery_and_refresh_only_failure`, `::test_calendar_failure_does_not_request_mail_delivery_test` | Source problems are per source and clear on recovery. | A-1, A-2 |
| `::test_failed_briefing_gives_brief_user_notification_instruction` | A failed tick never fails the tool. | A-7 |
| `test_scheduled_guard` | One session and schedule record, held by the harness. | A-10 |
