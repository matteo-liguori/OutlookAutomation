# Windows setup

Use Python 3.12+ and Classic Outlook configured with the intended accounts.
From the project folder:

```powershell
python setup_outlook.py --name 'Your name' --work-store 'you@example.com'
.\monitor.ps1 outlook status
.\tools\test_harness.ps1
```

For additional source switches such as `-WorkStore`, `-PersonalStore` and Google/iCloud
options, use the internal `monitor/adapters/windows/Initialize-OutlookAutomation.ps1`
only on a fresh installation. Never initialise over existing records.

Complete a supervised read-only pass, then establish filing and sending preferences.
Edit private `customisations.yaml`; keep identities/backend bindings in the private
connection profile. [Customisation](../customisation.md), [automation](../automations.md)
and [updating](../updating.md) describe the normal workflow. Helpers are implementation
internals; agents use the common `monitor.ps1 outlook` commands.
