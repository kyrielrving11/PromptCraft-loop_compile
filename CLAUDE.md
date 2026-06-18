# CLAUDE.md

This is the PromptCraft repository — a suite of prompt-engineering tools
for AI coding agents (CodeBuddy / Codex / Claude Code).

**Version:** v2.5 | **Tests:** 181 passing | **Python:** stdlib only

## Quick Start

```bash
# Run all tests
python tests/test_scripts.py

# Create a test vault
echo '{"task_id":"test","user_intent":"test save"}' | python skills/prompt-memory/scripts/checkpoint.py

# Search vault (auto-merges global + project)
python skills/prompt-memory/scripts/hydrate.py --query "test save"

# Save to global vault (cross-project)
echo '{"task_id":"org-standard","user_intent":"all contracts must pass Certora"}' | \
  python skills/prompt-memory/scripts/checkpoint.py --global
```

## Project Layout

```
skills/
├── prompt-craft/          # Core 6-step workflow (SKILL.md)
│   └── references/        # routing-matrix, build-checklist
├── prompt-memory/         # Dual-storage vault I/O + federation
│   ├── scripts/           # checkpoint.py, hydrate.py
│   └── references/        # vault-schema (incl. federation + feedback schemas)
├── prompt-techniques/     # Reference catalog of 7 techniques (SKILL.md)
│   └── references/        # zero-shot, few-shot, cot, step-back, least-to-most, tot
├── prompt-review/         # Quality audit with technique-specific checks (SKILL.md)
│   └── references/        # review-checklist
└── promptcraft-bridge/    # Trigger-only wrapper → PromptCraft sub-agent (SKILL.md)
    └── references/        # when-to-invoke.md — heuristics for triggering
promptcraft-agent/
├── subagent_adapter.py    # Unified entry point — 5-mode routing + health report
├── engine.py              # Outer loop manager — 5 invoke_* methods + silent analyze
├── builder.py             # Single-build pipeline (8-section prompt)
├── protocol.py            # I/O schemas (6 Mode values, SubagentOutput, etc.)
├── health_report.py       # HealthReport dataclass + threshold gating
├── context.py             # EngineContext — 3-layer shared state container
├── boundary.py            # Execution boundary — 5-layer defence-in-depth guards
├── circuit_breaker.py     # 3-state circuit breaker (CLOSED/HALF_OPEN/OPEN)
├── loop.py                # CLI entry point for testing
├── AGENT.md               # Claude Code sub-agent definition
└── tools/                 # Five-engine tool system
    ├── base.py            # Tool / ToolResult base + safety attributes
    ├── personalization.py # Skill overlay injection
    ├── prompt_build.py    # Full 8-section prompt generation (fallback)
    ├── feedback_collect.py # Explicit + implicit feedback capture
    ├── pattern_analysis.py # N-execution aggregate analysis
    └── skill_advisor.py   # Evolution/creation suggestions
.claude/agents/
└── promptcraft.md         # Sub-agent registration
docs/
└── AGENT_ARCHITECTURE.md  # Full plan for evolving to Sub-Agent model
tests/
├── test_scripts.py           # 48 unit tests (checkpoint, hydrate, federation)
├── test_health_report.py     # 26 tests (thresholds, stall, consistency, compact_str)
├── test_subagent_adapter.py  # 16 tests (routing, parsing, formatting, E2E)
├── test_engine_modes.py      # 19 tests (5 invoke_* + maybe_silent_analyze)
├── test_integration.py       # 12 tests (full closed-loop workflows)
└── test_boundary.py          # 52 tests (5-layer guards, circuit breaker, tool safety)
```

## Key Features (v2.4)

- **Execution Boundary Module**: 5-layer defence-in-depth for the sub-agent: Input → Tool → Vault → Output → Circuit Breaker. Adapted from Claude Code's 7-layer permission system for a sub-agent whose threat model is knowledge pollution and trust-chain abuse, not shell injection.
- **Batch Processing**: Process multiple tasks in a single PromptCraft call — hydrate once, group by Skill match, execute in parallel (max 4 workers), aggregate results.
- **Proactive Health Signals**: Every response includes `proactive_signals` — vault context hints (similar tasks, common pitfalls) without changing the passive-trigger model.
- **Multi-Project Federation**: Two-tier vault — global (`~/.promptcraft/`) + project (`./.promptcraft/`)
- **Query Expansion**: LLM-generated cross-language keywords before Jaccard search (zero-code)
- **Execution Feedback Loop**: Structured quality scoring (1-5) written back to vault
- **GLOBAL Entry Injection**: GLOBAL entries always returned regardless of query match
- **Multi-Script Tokenizer**: CJK + Japanese Kana + Korean Hangul + Latin + Cyrillic

## Conventions

- Vault entries are append-only. New versions use `checkpoint.py --version-of`.
- Script output is always JSON to stdout. Errors use `{"status": "error", ...}`.
- `importance: GLOBAL` entries are always returned by hydrate.py — inject their
  constraints unconditionally into every session.
- Execution feedback uses `importance: REFERENCE` — consultable but not auto-injected.
- Encoding: UTF-8 for vault I/O; `utf-8-sig` for stdin/file input (handles Windows BOM).
- Path separators: forward slash in vault `md_path` values (`as_posix()`).
- Global vault: `~/.promptcraft/global_vault.json` — hydrate.py auto-merges.
- Use `checkpoint.py --global` for cross-project entries; `hydrate.py --no-global` to opt out.
- Execution boundary is FAIL-CLOSED: guards deny when uncertain. MODIFIES_SKILLS is bypass-immune hard-deny for all tools.
- Circuit breaker trips after 3 consecutive denials (OPEN), probes after cooldown (HALF_OPEN), resets on success (CLOSED).

## PromptCraft Sub-Agent (v2.2)

PromptCraft is available as a sub-agent (`promptcraft`). It handles prompt
engineering, skill personalization, and execution feedback collection.

### Quick Usage

```bash
# Generate a prompt (build mode — no matching Skill)
echo '{"task":"audit ERC20 token","mode":"build"}' | python promptcraft-agent/subagent_adapter.py

# Personalise a Skill (overlay mode — matching Skill exists)
echo '{"task":"audit contract","mode":"overlay","skill_name":"solidity-audit"}' | python promptcraft-agent/subagent_adapter.py

# Collect execution feedback
echo '{"task":"audit contract","mode":"feedback","feedback":{"output":"...","success":true}}' | python promptcraft-agent/subagent_adapter.py

# Run pattern analysis (when health report signals ->analyze)
echo '{"task":"audit patterns","mode":"analyze"}' | python promptcraft-agent/subagent_adapter.py

# Get skill advice (when health report signals ->advise)
echo '{"task":"suggest improvements","mode":"advise"}' | python promptcraft-agent/subagent_adapter.py

# Batch process multiple tasks
echo '{"mode":"batch","items":[{"task":"audit token","skill_name":"solidity-audit"},{"task":"write docs"}]}' | python promptcraft-agent/subagent_adapter.py
```

### Modes

| Mode | When | What It Returns |
|------|------|-----------------|
| overlay | Skill exists, needs personalization | Overlay constraints + health report + proactive signals |
| build | No matching skill | Full 8-section prompt + health report + proactive signals |
| feedback | After execution | Feedback confirmation + health report |
| analyze | Health report recommends it | Pattern analysis report |
| advise | Evolution/creation ready | Skill advice (suggestions only, no auto-modify) |
| batch | Multiple tasks | BatchSummary + per-item results + health report |

### Health Report Signals

Every call returns a compact health line: `[PromptCraft] records=N quality=X.X ->action`

- `->analyze` — >=10 records, run analyze mode for detailed insights
- `->advise` — >=20 records + high consistency, skill evolution/creation warranted
- `->break` — 3 consecutive no-improvement iterations, needs user intervention
- (no arrow) — normal operation, continue

### Architecture

The Engine has 5 public `invoke_*` methods (one per mode) plus `maybe_silent_analyze()`
which runs after every invocation — if >=10 feedback records, pattern analysis triggers
silently (vault write only, nothing returned to main agent).

The subagent_adapter.py is the single entry point — it routes to the appropriate
engine method, calls silent analysis, and returns a SubagentOutput with the health
report and payload.

## Memory

Persistent project memory at: `C:\Users\Dell\.claude\projects\C--Users-Dell-Desktop-PromptCraft-Skills\memory\`
- `MEMORY.md` — index
- `project-overview.md` — what PromptCraft is, current state
- `agent-architecture.md` — agent evolution plan
- `design-decisions.md` — key architectural decisions
