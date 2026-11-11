# Mac setup

Use Python 3.12+ and a client configured with the intended accounts:

```sh
python3 setup_outlook.py --smoke-test
python3 setup_outlook.py --name 'Your name' --work-store 'you@example.com'
.venv/bin/python -m monitor outlook status
```

For Thunderbird, follow its [bridge setup](../../monitor/adapters/thunderbird/README.md).
The private connection profile selects the backend and verified bindings per source.
Retained native Outlook has its own [status](../../monitor/adapters/macos/STATUS.md).
Use a supervised capability check before writes; preserve read state.

Both clients use `.venv/bin/python -m monitor outlook ...`; no separate agent workflow.
Private behaviour is in `customisations.yaml`, while actual grants and tasks stay in
SQLite. See [customisation](../customisation.md), [automation](../automations.md) and
[updating](../updating.md). KCL calendar connectivity remains unresolved in Thunderbird;
see its [validation](../../monitor/adapters/thunderbird/STATUS.md) for current limits.
