# 10 Numeric targets

From plan section 9. "Today" is the old harness at commit `6eaa706`. Each
target names what enforces it. Constants are in `src/chamberlain/limits.py`.

## Principle 1: minimal agent knowledge

| Measure | Today | Target | Enforced by |
| --- | --- | --- | --- |
| Standing prompt | 1,284 chars | under 400 chars (`STANDING_PROMPT_MAX`) | gate test (T-1) |
| Agent-facing tools | about 30 commands | 12 (`TOOL_COUNT`) | gate test (T-2) |
| Total guidance text | 34,584 chars, 40 topics | under 12,000 chars (`GUIDANCE_TOTAL_CHARS`) | gate test (A-12) |
| Largest guidance item | 2,604 chars | 600 chars (`RULE_MAX_CHARS`) | gate test (A-12) |
| Guidance per reply | unbounded | 800 chars (`GUIDANCE_REPLY_CHARS`) | selection (A-14 onward) |
| Guidance per typical mail pass | not measured | under 1,500 chars | replayed session |
| Guidance pushed by a trigger | 4 hard-coded | over 90 percent of rules | gate test (A-13) |
| Tokens for a typical morning pass | not measured | baseline in wave 2, then at most half | replayed session |
| Wrong or missing tool calls per replay | not measured | baseline, then falling | replayed session |

## Principle 2: ordered context and artifacts

| Measure | Today | Target | Enforced by |
| --- | --- | --- | --- |
| Tables that never expire | `meta`: 202 keys, growing | 0 (`TABLE_CLASSES` covers all) | gate test (S-6, M-1) |
| Open card size | 8.6 KB average, 27 KB largest | under 3,000 chars, hard cap (`CARD_MAX_CHARS`) | M-12 |
| Evidence rows per open card | about 113 | under 15 | `doctor --metrics`; sweeper M-3 |
| Files in the data root unknown to the harness | about 2,400 | 0 after the 14-day grace | sweeper M-26 |
| Artifacts without a live owner | not measurable | 0 after each sweep | sweeper property test |
| Stored bytes at 4, 12, 26 weeks | not tracked | flat after week 4, within 20 percent | `doctor --metrics` history |
| Largest tool reply | 12,000 chars | 6,000 chars (`MAX_REPLY_CHARS`) | app (E-2) |
| List page | 12,000 chars | 4,000 chars (`PAGE_CHARS`) | M-17 |

## Safety

| Measure | Today | Target | Enforced by |
| --- | --- | --- | --- |
| Duplicate or unreconciled sends | not tracked | 0, checked from the audit log | write-engine property tests; `doctor --check audit` |
| Re-dispatch of an uncertain write | prevented by convention | impossible by construction | W-8 |
| Grants activated without human confirmation | all | 0 for standing grants and new recipients | G-12, G-14 |
| Grant rows deleted on revoke or spend | all | 0 | G-2 |
| Reads that change read state | guarded per adapter | 0 | contract suite (P-3) |

## Engineering

| Measure | Target |
| --- | --- |
| Gate (`python -m chamberlain.dev check`) | green before every report and every merge |
| Type checking | pyright strict, 0 errors, no `# type: ignore` without a reason |
| Gate wall time on the fake | under 120 seconds |
| `ValueError` raised for agent-visible conditions | 0 (E-14) |
| Function-local imports | 0 outside optional dependencies (D-13) |

## `doctor --metrics` output (fixed keys)

`open_cards`, `open_card_chars_avg`, `open_card_chars_max`,
`evidence_rows`, `evidence_per_open_card`, `artifacts_live`,
`artifact_bytes`, `artifacts_without_owner`, `orphans_quarantined`,
`db_bytes`, `data_root_bytes`, `largest_reply_chars_7d`,
`guidance_total_chars`, `uncertain_operations`, `grants_active`,
`grants_awaiting_human`.
