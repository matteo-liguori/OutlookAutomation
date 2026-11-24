# Board

The one place builders tell each other things. Read it at the start of every
working session; append to it whenever one of the cases below applies.

## Rules

1. **Append only.** New entries go at the bottom. Never edit or delete an
   entry, including your own; add a follow-up entry instead.
2. **One entry, one subject.** Use this heading form:

   ```
   ## 2026-11-25 14:10 UTC | track | KIND | short subject
   ```

   `track` is `architect`, `memory`, `attention`, `writes`, `thunderbird`
   or a wave-2 name. `KIND` is one of:

   | Kind | Use it when |
   | --- | --- |
   | `CONTRACT-CHANGE` | You changed, or need to change, a shared file (`docs/OWNERSHIP.md`). Name the files, the rule IDs, the reason and who is affected. |
   | `INTERFACE` | You added or changed something in your own code that another track calls or reads (a table column you write, a service method, a module name). |
   | `BLOCKER` | You cannot proceed without another track or the owner. Say exactly what you need and what you are doing meanwhile. |
   | `QUESTION` | You need a ruling on what a contract means. Say how you are reading it meanwhile. |
   | `DECISION` | The answer to a question, or a choice the contract left open. |
   | `HANDOFF` | Your track, or a milestone of it, is done: what exists, the gate result, what is not done, where the next person starts. |
   | `ACK` | You have read an entry that affects you and adapted to it. Quote its heading. |

3. **Additive contract changes** (a new optional field, error code, event
   kind, system key or limit) may land at once: post the entry and commit
   the change in its own commit. **Breaking contract changes** wait for an
   `ACK` from every affected track, or a `DECISION` from the architect or
   owner.
4. **Keep it short and factual**: what, where (paths), since which commit.
   No status diaries. No "still working on it".
5. **Never post private data**: no mail content, addresses, names from the
   owner's data, tokens or paths into `OutlookAutomation-data`.
6. Do not wait on the board. While blocked, work against the fake provider
   and the service doubles (`src/chamberlain/app/doubles.py`).
7. Commit board entries with the change they describe, or on their own;
   resolve merge conflicts in this file by keeping both sides in time order.

## Entries

## 2026-11-24 12:00 UTC | architect | HANDOFF | Wave 0 skeleton and contracts

- Contracts are in `docs/contracts/` (start at `00-index.md`). Briefs for the
  four wave-1 tracks are in `docs/waves/wave-1/`.
- The gate is `python -m chamberlain.dev check` and is green on the skeleton.
- Built and usable now: `schema/`, `domain/` (shared modules), `ports/`,
  `store/` with migration `0001`, `connectors/fake/`, `testing/` (contract
  suites and rigs), `app/` (entry point, service protocols and doubles),
  CLI and MCP stubs.
- Not built: every handler except `doctor`; real `Services`; any real
  connector; the viewer.
- Integration of the four tracks into `app/wiring.py` is a separate step
  after wave 1, done by the architect.

## 2026-11-24 12:00 UTC | architect | DECISION | Choices the plan left open

1. Operations have two states the plan's diagram lacks: `proposed` (intent
   stored, grant pending) and `cancelled` (04-writes.md).
2. `propose` is the only way to attach an in-chat approval
   (`operation` + `approval_evidence`); there is no thirteenth tool. Human
   confirmation is mandatory for standing grants and unknown recipients, and
   is also the default for sends to known recipients; other writes default
   to agent-recorded evidence (05-grants.md, G-12). The owner may revisit
   these defaults.
3. `forget` revokes a grant and cancels an undispatched operation (T-11).
4. Terminal grants and operations are compacted, and the compact rows leave
   with the 730-day audit window. "Never deleted" means never deleted by
   use, revocation or expiry (G-2).
5. Type checker: pyright (strict on `src`). Packaging: setuptools and pip;
   `uv` is not available on the build machine.
6. The MCP stub is a dependency-free JSON-RPC loop. Adopting the official
   SDK is a wave-2 decision.
7. Tracks meet through the SQL schema (one writer per table) and the
   protocols in `app/services.py`; they never import each other
   (`tests/test_layout.py`).
8. Migration numbers are reserved per track (09-storage.md, S-12).
