# Configuration consumers

Shared defaults are in defaults/behaviour.yaml; private customisations override
them, with allowed card overlays and version-approved extension settings applied
by the normal invocation snapshot. No installation IDs belong in shared defaults.
`config show --explain` shows the effective settings and their origin.

| YAML fields | Runtime consumers |
| --- | --- |
| schema_version | Configuration validation/migration |
| timezone | Local scheduling/DST, activation, calendar windows, date warnings, review/follow-up clocks, backup periods and report text |
| schedule.morning | Bound daily app recurrence, activation and generated startup instructions |
| schedule.continuations | Same-day future plans, app recurrence reconciliation, activation/readback validation and follow-up instructions; empty list disables them |
| schedule.title | Activation, report title verification, stale-chat comparison and managed follow-up names |
| schedule.roundup_weekday, roundup_start, roundup_end | Run/report eligibility and rendered roundup guidance; these gate the morning report, not a separate automation |
| schedule.automations | Explicit per-installation ownership bindings for the app-tool synchroniser |
| reviews.card_days, followup_days, hour | Card and action-review defaults and follow-up calculation |
| reviews.urgent_max_days | Urgent review validation and rendered policy guidance |
| reviews.upgrades_days, upgrades_after_hour | Improvement-review eligibility and next review |
| maintenance.closed_thread_days | Closed-card expiry and retention guidance |
| maintenance.daily_backups, weekly_backups, special_backups | Verified backup rotation and displayed retention policy |
| maintenance.cache_days, temporary_days, development_days | Owned local-file retention planning and cleanup |
| maintenance.collector_cache_hours | Disposable collector/snapshot expiry and guidance |
| maintenance.small_attachment_bytes | Backend/cache attachment download decisions |
| permission_defaults.* | Effective agent-facing permission policy in permissions/calendar/filing guidance; actual sends/calendar/custom actions still require applicable SQLite grants and verified outcomes |
| instructions.* | Relevant help/guidance, rendered from effective configuration |
| templates.report | Rendered report guidance |
| templates.morning, templates.followup | Managed app prompt sections; follow-up date/timezone are formatted from its binding |
| updates.enabled, interval_hours, remote, tag_prefix | Tagged-update eligibility, caching and Git remote selection |

Explicit `{{field.path}}` placeholders render configuration into instructions and
templates. `{{schedule.roundup_day}}` renders the configured weekday. Literal user
text is preserved: changing a schedule must not rewrite an unrelated appointment
time in a user's instructions. Record limits, paging caps, filing deduplication and
authorization checks are separate protocol safeguards, not implicit YAML settings.

## App reconciliation boundary

`schedule sync` reads only explicitly bound app definitions. The shared JS executor
applies its update/delete operations through the supported app tool, after a fresh
preflight checks for changed YAML/app state. A second sync verifies actual local app
readback; pending work is reissued rather than acknowledged on display. No app TOML
is written by the harness. Paused work stays paused, missing morning tasks require
review, and deleted/completed follow-ups are never recreated. There is no substitute
scheduler or background model loop. An agent must be running to use the app tool;
raw YAML edits therefore reconcile at the next harness interaction, not at file-save
time in an otherwise idle installation.

Named-zone daily rules preserve wall time across DST. Bounded follow-ups are UTC
instants for the bound local day; nonuniform minutes use exact RDATEs, avoiding the
Cartesian-product problem of multi-hour/multi-minute RRULEs. End-of-day or disabled
continuations delete the owned heartbeat rather than moving it to another day.
