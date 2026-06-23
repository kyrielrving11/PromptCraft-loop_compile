# CLAUDE.md — PromptCraft-loop_compile

This is the PromptCraft-loop_compile repository — a **Loop-Time Intelligence Layer** for AI coding agents. It compiles per-iteration prompts with structured memory, constraint inheritance, and drift correction — called once per agent loop iteration to maintain long-horizon cognitive stability.

**Version:** v3.5.2 | **Tests:** 189 passing | **Python:** stdlib only

## Quick Start

Deploy PromptCraft as a Claude Code sub-agent in your project:

```bash
# 1. Copy the 3 core directories into your project
cp -r loop-compiler/ skills/ .claude/ <your-project>/

# 2. Initialize the vault
cd <your-project>
echo '{"task_id":"init","user_intent":"promptcraft initialized"}' \
  | python skills/prompt-memory/scripts/checkpoint.py

# 3. Primary: loop_compile
echo '{"mode":"loop_compile","loop_id":"test","round":1,"goal_id":"audit","task":"Audit ERC20 token"}' \
  | python loop-compiler/subagent_adapter.py
```

## 3 Modes (v3.5)

| Mode | When | What It Returns |
|------|------|-----------------|
| **loop_compile** | Every agent loop iteration | Compiled prompt + recompile_level + loop_health + task_alignment + loop_objective |
| **feedback** | After execution | Quality score → vault persistence |
| **review** | Audit prompt quality | Structural checks + constraint compliance |

`build` is an internal path (loop_compile L2 delegation) — not an exposed mode.

## Recompile Levels (loop_compile)

- **L0 Fast Path** — goal_id unchanged, no new failures/constraints → reuse cached prompt. Auto-escalates to L2 when no cached prompt available.
- **L1 Patch** — new constraints, new failures, or repair signals → patch previous prompt
- **L2 Full Recompile** — round 1, goal_id changed, plan_source provided, strategy collapse → full build. force_level cannot override round 1 or plan_source.

## Project Layout

```
loop-compiler/
├── subagent_adapter.py    # Unified entry point — 3-mode routing
├── engine.py              # Lifecycle + vault I/O + circuit breaker + YAML frontmatter dual-write
├── loop_compiler.py       # Pure-function compiler: decide_level + advisories + L0/L1/L2
├── builder.py             # Technique router (keyword + adaptive) + quality scoring
└── protocol.py            # I/O schemas (19 types, v3.5)

skills/
├── prompt-memory/         # Dual-storage vault I/O + federation
│   ├── scripts/           # checkpoint.py, hydrate.py, vault_io.py (shared I/O)
│   └── references/        # vault-schema
└── prompt-techniques/     # Reference catalog of 7 techniques
    └── references/        # zero-shot, few-shot, cot, step-back, least-to-most, tot

tests/
├── test_loop_compiler.py     # 94 tests: gates, advisories, L0/L1/L2, constraint retirement, rolling summary, adaptive routing
├── test_scripts.py           # 49 tests: checkpoint, hydrate, federation
├── test_engine_modes.py      # 22 tests: invoke_*, YAML frontmatter, lineage md
├── test_subagent_adapter.py  # 7 tests: routing, parsing, formatting
└── test_integration.py       # 9 tests: closed-loop workflows, circuit breaker, feedback backfill
```

## Key Features (v3.5)

- **loop_compile**: Primary entry point — per-iteration prompt compiler with three incremental recompilation levels. L0 reuses the actual cached prompt from the previous round (retrieved from Markdown), not a placeholder.
- **Loop Objective Anchoring**: Auto-generated stable reference at round 1 via 3 sources (explicit, plan_source file extraction, auto from task)
- **4-Gate Hard Routing**: force_level override (never overrides round 1 or plan_source — those are hard L2 triggers), first-call/plan_source, goal_id stability, failure/constraint → determines compile level. Soft advisories (task alignment, loop health, repair cues, forward hints) never block
- **Dual-Key Goal Identity**: goal_id (stable semantic key) + goal_text_hash (auxiliary drift detection). L0/L1 gating uses goal_id; hash divergence warns but doesn't force L2
- **Constraint Retirement (v3.5)**: Stale constraints with no activity signal for 3 consecutive rounds are automatically retired to `constraints_retired` — prevents prompt bloat across long loops
- **Rolling Summary (v3.5)**: Deterministic cross-round knowledge distillation from last 5 rounds — quality trajectory, what worked, recurring issues, key lessons — injected into L1/L2 prompts
- **Adaptive Technique Routing (v3.5)**: Quality-driven fallback from keyword heuristic — when the same technique yields 2+ consecutive low-quality rounds, rotates to the next technique in the fallback chain (e.g., zero-shot → few-shot → few-shot-cot)
- **Feedback→Lineage Quality Backfill (v3.5.1)**: Feedback quality scores written with loop-aware task_ids and merged into lineage entries during hydrate — enables full end-to-end adaptive routing
- **Cross-Round Vault Memory**: Lineage persisted after each round → vault → hydrated next round → get_previous_round() reads prior state
- **Task Alignment**: Validates Agent-proposed next tasks against Loop Objective — advisory only, distinguishes legitimate task evolution from goal drift
- **Bidirectional Lineage Storage**: Vault JSON (structured, searchable, primary) + Markdown frontmatter (human-readable, git-friendly, fallback read path). `_hydrate_loop_context` reads JSON first, falls back to scanning `.md` files.
- **Engine Metrics**: Observable silent-failure counters (vault write errors, subprocess timeouts) surfaced via health line
- **Multi-Project Federation**: Two-tier vault — global (`~/.promptcraft/`) + project (`./.promptcraft/`)
- **8 Core Mechanisms**: Loop Objective anchoring, cross-round memory, L0/L1/L2 decisions, constraint retirement, rolling summary, adaptive routing, technique routing, drift correction

## Conventions

- Vault entries are append-only. New versions use `checkpoint.py --version-of`.
- Script output is always JSON to stdout. Errors use `{"status": "error", ...}`.
- Verify all changes with: `python -m unittest discover -s tests -p "test_*.py"`
- Encoding: UTF-8 for vault I/O; `utf-8-sig` for stdin/file input (handles Windows BOM).
- Path separators: forward slash in vault `md_path` values (`as_posix()`).
- Markdown paths: colons in loop_id are replaced with `-` for filesystem safety (e.g., `loop:smoke` → `.promptcraft/prompts/loop-smoke/r1.md`).
- Global vault: `~/.promptcraft/global_vault.json` — hydrate.py auto-merges.
- Use `checkpoint.py --global` for cross-project entries; `hydrate.py --no-global` to opt out.

## loop_compile Usage

```bash
# Round 1 — full L2 compile with auto loop_objective
echo '{"mode":"loop_compile","loop_id":"audit-erc20","round":1,"goal_id":"audit-erc20","task":"Audit ERC20 token for security vulnerabilities","domain":"solidity-security"}' | python loop-compiler/subagent_adapter.py

# Round 2 — L1 patch (same goal_id, new constraint)
echo '{"mode":"loop_compile","loop_id":"audit-erc20","round":2,"goal_id":"audit-erc20","task":"Check approve race condition","constraints_from_plan":["check flash loans"],"new_since_last_round":"fix the approve race"}' | python loop-compiler/subagent_adapter.py

# Force level override
echo '{"mode":"loop_compile","loop_id":"test","round":5,"goal_id":"audit","task":"audit token","force_level":"l0"}' | python loop-compiler/subagent_adapter.py

# Feedback
echo '{"task":"audit contract","mode":"feedback","feedback":{"output":"...","success":true}}' | python loop-compiler/subagent_adapter.py

# Review
echo '{"mode":"review","prompt_id":"audit:r3"}' | python loop-compiler/subagent_adapter.py
```

## Health Line

Every response includes a compact health line: `[PC: N records, normal]`

- `normal` — normal operation
- `STALLED` — 3 consecutive no-improvement iterations, needs user intervention
- Silent-failure counters appended when non-zero: `write_err`, `write_timeout`, `sub_timeout`, `cache_miss`
- (no arrow) — continue
