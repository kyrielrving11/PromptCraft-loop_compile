# PromptCraft-loop_compile

[中文文档](README.zh-CN.md)

PromptCraft-loop_compile is a **Loop-Time Intelligence Layer** for AI coding agents
(Claude Code / Codex / CodeBuddy). Its primary job: compile per-iteration prompts
for long-running agent loops — with structured memory, constraint inheritance,
drift correction, and incremental recompilation (L0/L1/L2).

> **v3.5.1** — 3-mode interface. 8 core mechanisms. Vault-backed cross-round memory.
> Constraint retirement, rolling summary, adaptive technique routing.
> 186 tests. Python stdlib only.

---

## Core Concept

```
Loop Runtime (Claude Code /loop, cron, etc.)
  │
  ├─ Round N:   call PromptCraft loop_compile → get compiled prompt → execute → feedback
  ├─ Round N+1: call PromptCraft loop_compile (reads vault, patches or rebuilds) → execute
  └─ ...

PromptCraft is NOT the Loop Runtime. It is the intelligence layer that the
Runtime calls when it needs a prompt that knows what happened last round.
```

## 3 Modes

| Mode | Trigger | Returns |
|------|---------|---------|
| **loop_compile** | Every agent loop iteration | Compiled prompt + recompile_level (L0/L1/L2) + loop_objective + loop_health + task_alignment |
| **feedback** | After execution | Quality score → vault persistence |
| **review** | Audit prompt quality | Review report with structural checks |

`build` is an internal path (loop_compile L2 delegates to `builder.py` for technique routing) — not an exposed mode.

## Recompile Levels

| Level | Trigger | What Happens |
|-------|---------|--------------|
| **L0 Fast Path** | goal_id unchanged, no new failures/constraints | Reuse actual cached prompt from Markdown (not placeholder) |
| **L1 Patch** | New constraints, failures, or repair signals | Patch previous prompt with deltas; auto-retires stale constraints |
| **L2 Full Recompile** | Round 1, goal_id changed, plan_source, strategy collapse | Full hydrate + adaptive route + rolling summary + build |

**Hard Gates** (can change compile level): force_level override, first-call/plan_source, goal_id change, explicit failure/constraint.

**Soft Advisories** (warnings only, never block): task alignment vs Loop Objective, loop health (drift, constraint integrity, strategy stability), repair cue detection, forward hints from vault.

**v3.5 additions**: L1 auto-retires constraints silent for 3+ rounds; L1/L2 inject rolling summary (quality trajectory, recurring issues, key lessons from last 5 rounds); L2 uses adaptive technique routing (quality-driven fallback from keyword default).

## Quick Start

```bash
# 1. Copy core directories into your project
cp -r loop-compiler/ skills/ .claude/ <your-project>/

# 2. Initialize vault
cd <your-project>
echo '{"task_id":"init","user_intent":"promptcraft initialized"}' \
  | python skills/prompt-memory/scripts/checkpoint.py

# 3. Primary: loop_compile
echo '{"mode":"loop_compile","loop_id":"test","round":1,"goal_id":"audit-erc20","task":"Audit ERC20 token for security vulnerabilities"}' \
  | python loop-compiler/subagent_adapter.py

# 4. Feedback
echo '{"task":"audit contract","mode":"feedback","feedback":{"output":"done","success":true}}' \
  | python loop-compiler/subagent_adapter.py
```

## Architecture

```
Main Agent (Claude Code / Codex)
  │
  └─ PromptCraft Sub-Agent
        │
        └─ Python layer (pure function + lifecycle)
            ├─ loop_compiler.py    ← decide_level + advisories + L0/L1/L2
            ├─ builder.py          ← technique router (keyword + adaptive) + quality scoring
            ├─ engine.py           ← lifecycle + vault I/O + circuit breaker + YAML dual-write + feedback backfill
            ├─ protocol.py         ← I/O schemas (19 types)
            └─ subagent_adapter.py ← unified entry point, 3-mode routing
```

## Key Features (v3.5)

- **loop_compile**: Per-iteration prompt compiler — L0/L1/L2 incremental recompilation with 4 hard gates
- **Loop Objective Anchoring**: Auto-generated stable reference at round 1 via 3 sources — prevents goal drift across rounds
- **Constraint Retirement (v3.5)**: Stale constraints with no activity signal for 3 consecutive rounds auto-retire — prevents prompt bloat
- **Rolling Summary (v3.5)**: Deterministic cross-round knowledge distillation — quality trajectory, what worked, recurring issues, key lessons — injected into L1/L2 prompts
- **Adaptive Technique Routing (v3.5)**: Quality-driven fallback from keyword heuristic — 2+ consecutive low-quality rounds trigger rotation (e.g., zero-shot → few-shot)
- **Feedback Quality Backfill (v3.5.1)**: Feedback quality scores written with loop-aware task_ids, merged into lineage entries during hydrate — enables end-to-end adaptive routing
- **Cross-Round Vault Memory**: Lineage persisted each round → vault → hydrated next round → get_previous_round()
- **Dual-Key Goal Identity**: goal_id (stable semantic key) + goal_text_hash (drift detection, advisory only)
- **Task Alignment**: Validates Agent-proposed next tasks against Loop Objective — distinguishes legitimate evolution from drift
- **Bidirectional Lineage Storage**: Vault JSON (primary, searchable) + Markdown frontmatter with YAML headers (human-readable, git-friendly, fallback read path). L0 cache reuses actual cached prompts from Markdown files.
- **4 Soft Advisories**: Task alignment, loop health, repair cues, forward hints — all warnings, never hard gates
- **Circuit Breaker**: Pure-function trend detection — 3 consecutive no-improvement rounds → STALLED. Counter updated only on feedback events.
- **Multi-Project Federation**: Two-tier vault — global (`~/.promptcraft/`) + project (`./.promptcraft/`)
- **Append-only Vault**: Full version history, rollback support, bidirectional storage (JSON + Markdown frontmatter)
- **Shared vault I/O**: `vault_io.py` — single source of truth for `read_vault` / `write_vault`, used by both checkpoint and hydrate
- **186 tests**, Python stdlib only, zero external dependencies

## Project Structure

```
PromptCraft-loop_compile/
├── loop-compiler/
│   ├── subagent_adapter.py    # Unified entry point, 3-mode routing
│   ├── engine.py              # Lifecycle + vault I/O + circuit breaker + YAML dual-write
│   ├── loop_compiler.py       # Pure-function compiler: gates + advisories + L0/L1/L2
│   ├── builder.py             # Technique router (keyword + adaptive) + quality scoring
│   └── protocol.py            # I/O schemas, 19 types
├── skills/
│   ├── prompt-memory/         # Dual-storage vault I/O + federation
│   │   └── scripts/           # checkpoint.py, hydrate.py, vault_io.py
│   └── prompt-techniques/     # Catalog of 7 techniques
├── tests/
│   ├── test_loop_compiler.py  # 94 tests: gates, advisories, L0/L1/L2, constraint retirement, rolling summary, adaptive routing
│   ├── test_scripts.py        # 49 tests: checkpoint, hydrate, federation
│   ├── test_engine_modes.py   # 22 tests: invoke_*, YAML frontmatter, lineage md
│   ├── test_subagent_adapter.py # 7 tests: routing, parsing, formatting
│   └── test_integration.py    # 9 tests: closed-loop workflows, breaker, feedback backfill
├── CLAUDE.md                  # Project conventions
└── README.md / README.zh-CN.md
```

## Design Principles

- **Python classifies, LLM generates** — technique selection is keyword heuristic; prompt writing is LLM-driven
- **Loop Objective is an anchor, not a planner** — it freezes what+why, doesn't decompose work
- **Soft advisories never block** — task_alignment, loop_health, repair cues are warnings, not hard gates
- **Enhance, don't replace** — Skills own the workflow, PromptCraft provides overlay
- **Fail-closed** — guards deny when uncertain
- **Never auto-modify Skills** — suggestions only, execution is the main agent's job
- **Append, never overwrite** — full version history preserved
- **Zero external dependencies** — plain filesystem, human-readable JSON/Markdown

## License

MIT License. See [LICENSE](LICENSE).
