# Watching how the assistant works, and the file guard

This page is for the owner. The binding text is in `docs/contracts/06-memory.md` (M-40) and
`docs/contracts/02-tools.md` (T-10c).

## The improvisation log

Chamberlain would like to know when the assistant keeps doing something outside its tools, because
that is a feature the harness could offer instead.

- After each scheduled pass, and otherwise at most once a day, the harness reads the assistant's own
  session transcripts: Claude Code's `projects/*/*.jsonl` and Codex's `sessions/**/*.jsonl`. It only
  looks at sessions that ran in this checkout, or that called a Chamberlain tool.
- For every call that was not a Chamberlain tool it keeps the tool name and, for a shell command, a
  few words: the program and sometimes one sub-command (`git status`, `python -m pytest`). It never
  keeps an argument, a path, a number, what a command printed, mail text or file contents. A script
  run by path is logged as `(script)`.
- Counts are added up by pattern, with the time last seen. At most 500 patterns are kept, and a
  pattern not seen for a year is dropped.
- The log is never in the daily report and never on the viewer's front page.
- It reads the real home folders only through `review/homes.py`. Tests point that at an empty
  folder (`CHAMBERLAIN_AGENT_HOME`).
- Not verified against live transcripts: how Codex names Chamberlain's MCP tools. The checkout test
  and the command-line form still catch those sessions.

## The review

Every `review.every_days` (default 21), and only when the log has a pattern it did not have at the
last review, the harness raises one item, `harness.review`. It comes after all other work and after
the pass report. The assistant opens it, reads the list, and writes what it finds with `keep` (kind
`finding`): an `idea` (a harness feature that would remove the improvising) or something `worrying`.

- Only a worrying finding reaches you: once, as a line in the next pass report.
- Ideas stay in the store (`review_findings`, a year) for whoever maintains the harness.
- Marking the item handled ends the round; one left alone lapses after 14 days.
- Turn it all off with `review: {enabled: false}` in `config/connections.yaml`.

## The Claude file guard

This is an accident guard, not a security boundary.

```
.venv/Scripts/python -m chamberlain.review.claude_guard add --data <data root>
.venv/Scripts/python -m chamberlain.review.claude_guard check --data <data root>
.venv/Scripts/python -m chamberlain.review.claude_guard remove --data <data root>
```

`add` puts two deny rules in Claude Code's user settings (`~/.claude/settings.json`):
`Read(//<data root>/**)` and `Edit(//<data root>/**)`. Claude Code applies a Read rule to Grep and
Glob as well, and an Edit rule to Write and notebook edits. Your other settings and rules are left
as they are, adding twice changes nothing, and `remove` takes out only what `add` put in. A settings
file that is not valid JSON is left untouched and reported.

- Use `--where project` to write the checkout's `.claude/settings.local.json` instead. Never the
  project's `settings.json`: it is committed to Git and would carry your private path.
- The guard notes in `<data root>/config/claude-guard.json` which file it changed. `doctor`
  (check group `access`) warns when that file is missing or lacks a rule.
- It stops Claude Code's file tools only. It does not stop a shell command (`cat`, `Get-Content`,
  a Python script), and **Codex has no equivalent**. The viewer token (`run/viewer.token`) lives
  inside the data root, so it is covered by the same rule.
- Not verified here: the rule text was checked against Claude Code's published permissions page,
  not against a running Claude Code.
