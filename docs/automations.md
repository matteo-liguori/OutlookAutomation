# Daily checks and follow-ups

After a supervised pass works, agree the schedule. `config show` supplies the
current schedule and `templates.morning` / `templates.followup` prompts; edit
private `customisations.yaml` to change them. The shared defaults are 09:00 daily
with optional 14:00/19:00 same-day checks in Europe/London.

Use the morning template for a standalone automation with this project as its
working directory. Each morning starts a new chat. Its first full `briefing`
supplies the activation checklist; use `help activation` for naming and scheduling.

For follow-ups, fill the template's `{date}` and `{timezone}` from the harness and
target that morning chat. `schedule plan` supplies only still-future same-date
instants. Create the bounded app schedule with supported tools, read it back and
use `schedule verify`. Do not duplicate schedules or substitute another day.
On a user reply, `briefing --user-active` retries any pending setup. Follow
`help mail-pass` and `help reporting` for each round; disclose incomplete coverage.

The app launches the assistant; SQLite reminders alone do not. The harness checks
setup but does not itself rename chats or create app automations. Keep the computer
on and the desktop app running for local tasks. See the
[official scheduling guide](https://learn.chatgpt.com/docs/automations?surface=app).
Use one scheduling installation per data root. An isolated worktree needs its own
environment and explicit private-data access. Other assistants need equivalent
local scheduling and chat targeting. Verify the first scheduled report and stop
follow-ups after their final same-day check or cancellation.
