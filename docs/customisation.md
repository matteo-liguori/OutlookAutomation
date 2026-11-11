# Customisation

The shared engine lives in `monitor/`; public behaviour is in `defaults/behaviour.yaml`.
Setup copies those defaults to `customisations.yaml` in your private data root.
Edit that file for personal instructions, report templates, schedules and review or
maintenance defaults. Account bindings stay in `monitor/profile.json` beside SQLite.

`config show --explain` shows effective values and their source. `--case ID` includes
card overrides. `config validate` checks edits. Default updates refresh untouched
values; your edits win. Conflicts appear once in briefing and remain in `config review`.
Resolve one with `config review --input -`: `{"id":"...","decision":"keep"}` or
`"default"`. `config reset --key reviews.card_days` restores a shared default.

Cards can carry `customisations` for instructions, review policy, templates and
permission defaults. Use normal `case update` revision/evidence controls; omitted
customisations and executable action references survive routine card updates.
Actual send/calendar/execution grants remain in SQLite and require user authority.

## Executable actions and experiments

Private `extensions/ID/manifest.yaml` declares `id`, `kind` (`action` or `experiment`),
`enabled`, optional offset-qualified `expires_at`, and `timeout_seconds` (1–300).
Optional `settings` and per-card `card_overlays` temporarily overlay configuration.
`hooks` supports `activation` and `card_context`; `commands` maps `command:operation`
to a relative `module.py:function`. Extensions apply in ID order; later IDs win.

Each `actions.NAME` has `entrypoint` and a parameter mapping. Each parameter declares
`type` (`string`, `integer`, `boolean`, `object`, `array`) and optional `required`.
An entrypoint takes one context object and returns a JSON object. Action context
includes `card`, `action`, `parameters`, and `settings`. It may return `requests`
with Outlook `operation` and `payload`; ordinary approval checks still apply.
Activation context includes `state`, `user_active`, `now` and prior `observations`;
return `observations` to save a private namespaced result. Card context hooks return
`card` with its identity preserved. A command override returns its command result.

Use `extension list`, inspect the files, then `extension approve --input -` with
`id`, `hash` and user-authority `evidence_ids`. Code/manifest changes require a new
approval. Extensions are trusted local code, not a security sandbox.

An action row's `custom_action` contains `bundle`, `action`, `parameters`, and
`approval_id`. That permission must be card-bound with scope `operation: custom-action`,
the exact `bundle` and `action`. Run with `action run ID --input -`, providing a
unique `idempotency_key`. An interrupted execution remains blocked for reconciliation.
Use `action reconcile ID --input -` after checking its effects: provide that
`idempotency_key`, `evidence_ids`, outcome `complete` (and the verified `result`)
or `not-attempted`. Changing the run key alone cannot bypass an uncertain execution.

`extension disable ID`, expiry or removing the bundle restores ordinary behaviour
on the next invocation/view refresh. Completed external actions and receipts remain.
Keep generated observations out of bundle files so logging does not change code hashes.
