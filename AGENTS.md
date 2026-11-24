# Rules for builder agents

You are building Chamberlain v2 in this worktree, branch `chamberlain-v2`.

1. **Read first**: your brief in `docs/waves/`, then the contract files it
   names in `docs/contracts/`. The contracts are binding.
2. **Run the gate** before you report anything, and keep it green:
   `.venv/Scripts/python -m chamberlain.dev check` (Windows) or
   `.venv/bin/python -m chamberlain.dev check`.
3. **Respect ownership** (`docs/OWNERSHIP.md`). Change only your own
   directories. A shared file changes only with a `CONTRACT-CHANGE` entry on
   the board, in its own commit.
4. **Post to `docs/BOARD.md`**: interface changes, blockers, handoffs.
   Append only. Read it before each working session.
5. **Never touch** `C:\dev\OutlookAutomation` or
   `C:\dev\OutlookAutomation-data` except to read them, and never touch a
   real mailbox or calendar. Tests use the fake provider and temporary
   directories only.
6. **Code**: formatter-enforced; no semicolon-joined lines; imports at module
   top; raise `ChamberlainError`, not `ValueError`, for anything an agent can
   meet; take the time from the `Clock`; SQL only in your track's `repo.py`.
7. **Tests name the rule** they enforce (`test_w8_...`, `test_m21_...`).
   Port the intent of the old tests listed in
   `docs/contracts/11-ported-test-intent.md`, not their code.
8. **Commit small**, on this branch, without pushing. Do not install anything
   outside `.venv`. Do not add a dependency without a board entry.
