# Daily checks and follow-ups

After a supervised pass works, agree the schedule with the user. The current
harness uses Europe/London: a daily 09:00 pass, then optional same-day 14:00 and
19:00 checks in that day's chat. These are scheduled assistant runs, not Inbox
polling. Task deadlines and reply/chase dates stay in SQLite.

## Morning prompt

Use a standalone daily automation with the installed project as its working
directory. Each run starts a new chat. Save this prompt:

> You are an email, calendar and organisation assistant. Follow AGENTS.md in this project and start with the harness briefing.

`briefing` means `monitor.ps1 briefing` on Windows or
`.venv/bin/python -m monitor briefing` on Mac. Its first full call supplies the
current date, activation checklist and relevant instructions; keep those out of
the saved prompt. Follow `help activation` to verify the dated chat title and
set up remaining checks, then complete the mail/calendar pass and report.

## Same-day follow-up prompt

Target the morning chat. Replace `<YYYY-MM-DD>` with that day's London date:

> Continue the mail and calendar pass for <YYYY-MM-DD> in this chat. If that Europe/London date has passed, stop. Follow AGENTS.md and start with the harness briefing.

Use `schedule plan` for still-future 14:00/19:00 instants. Create the bounded
chat-targeted schedule through the assistant app's supported scheduling tools,
read it back and use `schedule verify`; avoid duplicate schedules. No next-day
substitutes. If setup fails, follow the activation deferral/retry instructions.
User replies start with `briefing --user-active` so pending setup is retried.
Each check reconciles new mail, due actions and calendar changes with existing
records. End the follow-up schedule after its final check or when cancelled.

## Installation and verification

The app supplies the wake-up; the harness supplies procedures and records. A
SQLite reminder alone does not launch an assistant, and the harness does not
rename chats or create app schedules itself. Other assistants need equivalent
local scheduling and chat-targeting support; disclose unavailable setup.

For Codex, use the local project with the configured private profile/data root,
keep the computer on and the desktop app running, and verify the first scheduled
run can access the environment and intended accounts. See the
[official scheduling guide](https://learn.chatgpt.com/docs/automations?surface=app).
An isolated worktree needs its own environment and explicit access to the same
private data. One installation should own the schedule to avoid duplicate passes.

Read back the morning timezone/cadence and follow-up chat target/UTC instants.
Check the first report for incomplete coverage. Scheduling grants no additional
mail/calendar permission. This guide is generic; account details, approvals,
app schedule IDs and local paths stay outside Git.
