# PromptCraft

[中文文档](README.zh-CN.md)

PromptCraft is a **prompt-engineering sub-agent** for AI coding agents
(Claude Code / Codex / CodeBuddy). It manages the full lifecycle of prompts
and skills: generation, personalisation, execution feedback, pattern analysis,
and evolution suggestions — backed by a persistent vault that improves across
sessions and projects.

> **v2.5** — Sub-agent architecture with 6 modes, 5-layer execution boundary,
> dual-storage vault, batch processing, proactive signals, vault-hydrate
> preflight gating, and 181 tests. Python stdlib only.

---

## Architecture

```
Main Agent (Claude Code / Codex)
  │
  ├─ promptcraft-bridge (trigger Skill)  ← when_to_use + vault hydrate preflight
  │     └─ delegates to PromptCraft sub-agent when warranted
  │
  └─ PromptCraft Sub-Agent (isolated context)
        │
        ├─ subagent_adapter.py   ← unified entry, 6-mode routing
        ├─ engine.py             ← lifecycle manager + circuit breaker
        ├─ boundary.py           ← 5-layer defence-in-depth
        ├─ circuit_breaker.py    ← denial tracking, 3-state machine
        └─ tools/                ← 5 specialised engines
              personalization / prompt_build / feedback_collect
              / pattern_analysis / skill_advisor
```

## Six Modes

| Mode | Trigger | Returns |
|------|---------|---------|
| **overlay** | Matching Skill + vault history | Domain-filtered constraints for Skill enhancement |
| **build** | No Skill + high-risk task, or vault baseline needed | Full 8-section structured prompt |
| **feedback** | After execution | Quality score + improvement notes |
| **analyze** | Health report signals `->analyze` | Pattern report from accumulated data |
| **advise** | Health report signals `->advise` | Skill evolution/creation suggestions |
| **batch** | Multiple tasks | BatchSummary + per-item results |

**Triggering model**: `when_to_use` (LLM semantic gating) → cheap vault hydrate
(`hydrate.py --query <task> --top 3`) → if relevant history or high-risk keywords
→ invoke overlay/build. Otherwise skip PromptCraft. No assess sub-agent round-trip.

Every response includes a compact **Health Report**: `[PC: 15 records, normal]`
and `proactive_signals` — vault context hints (similar tasks, common pitfalls).

## Quick Start

```bash
# 0. Cheap preflight — check vault for relevant history (no LLM cost)
python skills/prompt-memory/scripts/hydrate.py --query "audit security" --top 3

# 1. Build a prompt (no matching Skill, or high-risk task)
echo '{"task":"audit ERC20 token","mode":"build"}' \
  | python promptcraft-agent/subagent_adapter.py

# 2. Personalise a Skill (matching Skill + vault history found)
echo '{"task":"audit contract","mode":"overlay","skill_name":"solidity-audit"}' \
  | python promptcraft-agent/subagent_adapter.py

# 3. Record execution feedback
echo '{"task":"audit contract","mode":"feedback","feedback":{"output":"...","success":true}}' \
  | python promptcraft-agent/subagent_adapter.py

# 4. Batch-process multiple tasks
echo '{"mode":"batch","items":[{"task":"audit token","skill_name":"solidity-audit"},{"task":"write docs"}]}' \
  | python promptcraft-agent/subagent_adapter.py

# Vault I/O (standalone)
echo '{"task_id":"org-standard","user_intent":"all contracts must pass Certora"}' \
  | python skills/prompt-memory/scripts/checkpoint.py --global

python skills/prompt-memory/scripts/hydrate.py --query "audit security"
```

## Execution Boundary (5-Layer Defence-in-Depth)

Adapted from Claude Code's 7-layer permission system for a sub-agent whose
threat model is **knowledge pollution**, not shell injection.

| Layer | Guards | Hard-Deny Triggers |
|-------|--------|-------------------|
| 1 — Input | Injection detection, mode consistency | System-override patterns, mode-protocol mismatch |
| 2 — Tool | Per-tool safety attributes + `check_permissions()` | **MODIFIES_SKILLS** (bypass-immune) |
| 3 — Vault | Size cap (8KB), rate limit (50/session), dedup, GLOBAL quality ≥4 | Exceeding caps, GLOBAL with low quality |
| 4 — Output | Schema enforcement, sensitive-data scan, size cap | Schema violation, payload overflow |
| 5 — Breaker | Denial tracking, 3-state machine | 3 consecutive denials → OPEN (5 min cooldown) |

**Key rule:** `MODIFIES_SKILLS = False` for all tools. Skill modification is
bypass-immune — PromptCraft only suggests, the main agent executes.

## Project Structure

```
PromptCraft/
├── promptcraft-agent/
│   ├── subagent_adapter.py    # Unified entry point, 6-mode routing
│   ├── engine.py              # Lifecycle manager, 5 invoke_* methods
│   ├── builder.py             # Single-build pipeline (8-section prompt)
│   ├── protocol.py            # I/O schemas, 6 Mode values
│   ├── health_report.py       # HealthReport + threshold gating
│   ├── context.py             # EngineContext — 3-layer state container
│   ├── boundary.py            # 5-layer execution boundary guards
│   ├── circuit_breaker.py     # 3-state circuit breaker
│   ├── loop.py                # CLI entry point
│   ├── system_prompt.md       # 7-layer progressive system prompt
│   ├── AGENT.md               # Claude Code sub-agent definition
│   └── tools/                 # Five-engine tool system
│       ├── base.py            # Tool base + safety attributes
│       ├── personalization.py # Skill overlay injection
│       ├── prompt_build.py    # Full prompt generation (fallback)
│       ├── feedback_collect.py # Explicit + implicit feedback
│       ├── pattern_analysis.py # Aggregate pattern discovery
│       └── skill_advisor.py   # Evolution/creation suggestions
├── skills/
│   ├── prompt-memory/         # Dual-storage vault I/O + federation
│   │   ├── scripts/           #   checkpoint.py + hydrate.py
│   │   └── references/        #   vault schema
│   ├── prompt-techniques/     # Catalog of 7 techniques
│   │   └── references/        #   zero-shot through tree-of-thought
│   └── promptcraft-bridge/    # Trigger-only Skill → sub-agent delegation
│       └── references/        #   when-to-invoke heuristics
├── tests/
│   ├── test_scripts.py        # 48 tests (checkpoint, hydrate, federation)
│   ├── test_health_report.py  # 31 tests (thresholds, stall, consistency, proactive)
│   ├── test_subagent_adapter.py # 16 tests (routing, parsing, batch, E2E)
│   ├── test_engine_modes.py   # 19 tests (5 invoke_* + silent analyze + batch)
│   ├── test_integration.py    # 10 tests (full closed-loop workflows)
│   └── test_boundary.py       # 57 tests (5-layer guards, breaker, tools, batch input)
├── .claude/agents/            # Sub-agent registration
├── CLAUDE.md                  # Project conventions
└── README.md / README.zh-CN.md
```

## Key Features

- **Sub-Agent Architecture**: Isolated context, vault-backed persistence,
  cross-session improvement — wakes via trigger Skill with vault-hydrate preflight
- **Batch Processing**: Process multiple tasks in one call — hydrate once,
  group by Skill match, execute in parallel (max 4 workers)
- **Proactive Signals**: Every response includes vault-aware context hints
  (similar tasks, common pitfalls) without changing the passive-trigger model
- **5-Layer Execution Boundary**: Defence-in-depth adapted from Claude Code
  for a sub-agent's actual threat model (knowledge pollution, not shell injection)
- **Circuit Breaker**: 3-state machine (CLOSED → OPEN → HALF_OPEN) with
  denial tracking and automatic cooldown
- **Multi-Project Federation**: Two-tier vault — global (`~/.promptcraft/`)
  + project (`./.promptcraft/`)
- **Query Expansion**: LLM-generated cross-language keywords before Jaccard
  search (zero-code)
- **Execution Feedback Loop**: Structured quality scoring (1-5) written back
  to vault after every execution
- **Health Report**: Compact one-line signal — `[PC: N records, action=...]` —
  tells the main agent when to run analysis or advice
- **Skill-Advisor**: Data-backed evolution/creation suggestions — never
  auto-modifies Skills
- **Append-only Vault**: Full version history, rollback support, dual storage
  (JSON index + Markdown prompts)
- **Multi-Script Tokenizer**: CJK + Japanese Kana + Korean Hangul + Latin + Cyrillic

## Tech Stack

- **Python stdlib only** — no pip install, no venv
- **Dual storage** — JSON vault (metadata) + `.md` files (full prompts)
- **Two-tier federation** — global vault + project vault, auto-merged
- **Sub-agent model** — isolated context, trigger-based wake-up
- **Jaccard similarity** — multi-script tokenizer, zero external deps
- **Zero external API calls** — no embedding services, no proprietary APIs

## Design Principles

- **Enhance, don't replace** — Skills own the workflow, PromptCraft provides overlay
- **Fail-closed** — guards deny when uncertain; MODIFIES_SKILLS is bypass-immune
- **Health Report only** — internal vault state is never exposed to the main agent
- **Never auto-modify Skills** — suggestions only, execution is the main agent's job
- **importance = blast radius** — GLOBAL affects all projects, escalation requires data
- **Append, never overwrite** — full version history preserved
- **Zero external dependencies** — plain filesystem, human-readable JSON/Markdown

## License

MIT License. See [LICENSE](LICENSE).
