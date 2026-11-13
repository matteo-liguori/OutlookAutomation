# Alpha updates

`harness-update/v0.1.0-alpha.3` replaces the blocking chat-date guard with an
advisory for any mismatched dated chat. Commands continue normally, and a private
SQLite cooldown limits warnings to once per chat every 30 minutes.

`harness-update/v0.1.0-alpha.2` adds the shared core scheduled chat-date guard and
its `guard` / `help guard` commands, plus the action-owned cache and retention
enforcement already on main. The guard needs no dependency or database migration;
existing installation-specific title formats, timezone and private data are preserved.

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
