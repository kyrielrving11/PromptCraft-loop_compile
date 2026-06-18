# PromptCraft Agent

A specialized prompt-engineering sub-agent that works on an **on-demand
wake-up model**. The main agent wakes it when fuzzy requirements, multi-step
complexity, or high-risk operations would benefit from structured prompt
engineering.

## Quick Start

```bash
# Build the system prompt
python skills/promptcraft-agent/build_prompt.py --skills-dir skills

# With explicit platform
python skills/promptcraft-agent/build_prompt.py --skills-dir skills --platform "linux x86_64"

# Write to file
python skills/promptcraft-agent/build_prompt.py --skills-dir skills --output agent_prompt.txt
```

## Architecture

The system prompt follows a **7-layer progressive structure** adapted from
Claude Code's design:

| Layer | Purpose |
|-------|---------|
| 1. Identity & Boundaries | Who the Agent is, security limits |
| 2. System | Runtime facts, vault architecture |
| 3. Doing Tasks | Anti-pattern inoculation — precise "don'ts" |
| 4. Actions | Blast radius framework (importance = blast radius) |
| 5. Using Your Tools | Tool preference mapping, constraints |
| 6. Tone & Output Efficiency | Direct, JSON-only output |
| 7. Output Format | PromptCraftResponse schema + 8-section rules |

## Files

```
skills/promptcraft-agent/
├── SYSTEM_PROMPT.md       # 7-layer template with <placeholder> markers
├── build_prompt.py        # Builder: injects platform, skills_dir, date
└── README.md              # This file
```

## Variables

build_prompt.py replaces these placeholders at runtime:

| Placeholder | Source |
|-------------|--------|
| `<platform>` | `platform.system() platform.machine()` or `--platform` flag |
| `<skills_dir>` | `--skills-dir` flag (required) |
| `<date>` | `date.today().isoformat()` |

## Related Skills

- `prompt-memory` — Vault I/O scripts (checkpoint.py, hydrate.py)
- `prompt-techniques` — 6 technique reference files (loaded on demand)
- `prompt-craft` — Legacy Skills-based workflow (preserved)
- `prompt-review` — Quality audit checklists (preserved)
