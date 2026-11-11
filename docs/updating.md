# Alpha updates

Normal briefings check for tagged updates at most once per day, using a local
cache. `update check --force` checks now; `update status` shows cached results.
Only `harness-update/vMAJOR.MINOR.PATCH` tags (optionally `-alpha.N`, `-beta.N`,
or `-rc.N`) whose commit is on GitHub `main` qualify. Offline checks preserve work
and report their status. The checker never pulls, switches branches or runs code.

When an update is offered, inspect local Git changes and preserve unfinished work.
Review the tagged changes, update from `main`, install the platform dependency lock,
preview `config migrate`, apply any required migration, then run the smoke tests.
Preserve the private data root. Reconcile app schedules if effective scheduling changed.
The four-layer alpha moves internal helpers, reports schedule dates as `local_date`
with a timezone, and migrates behavioural profile fields into YAML. The migration
also updates existing Thunderbird native-host launchers. Rolling back across this
migration requires the combined private backup, not just an older Git checkout.

Your edited customisations remain in effect. When a changed default overlaps an
override, the harness shows the old override and new default for a decision.

To publish a noteworthy update, finish and test its commit on `main`, create an
annotated `harness-update/v...` tag there with a short change/migration summary,
and push that tag explicitly. Ordinary development commits do not trigger notices.

**Alpha** means features/architecture are still changing. **Beta** usually means
the intended feature set is substantially present and being stabilised. Both are
prereleases; this project currently uses alpha.
