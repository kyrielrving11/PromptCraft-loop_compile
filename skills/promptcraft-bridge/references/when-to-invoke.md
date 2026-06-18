# When to Invoke PromptCraft — Trigger Guide

This guide helps the main agent decide whether to invoke PromptCraft for a
given task. Use a cheap vault hydrate (`hydrate.py --query <task> --top 3`)
as the preflight gate; this document provides the static heuristics behind it.

## Decision Flowchart

```
Task received
  │
  ├─ Does when_to_use match? (complex/high-risk/cross-domain/PRD/feedback)
  │   └─ NO → skip PromptCraft, execute directly
  │
  └─ YES → Run cheap hydrate preflight:
        python hydrate.py --query "<task>" --top 3
        │
        ├─ Relevant vault entries found (Jaccard ≥ 0.3)?
        │   ├─ YES + has matching Skill → invoke mode="overlay"
        │   ├─ YES + no Skill → invoke mode="build"
        │   └─ NO → continue below
        │
        ├─ Task is high-risk (security/audit/crypto/deploy/migrat)?
        │   └─ YES → invoke mode="build" (establishes vault baseline)
        │
        └─ Otherwise → skip PromptCraft, execute directly
```

## Heuristic Rules

### SHOULD Invoke (high confidence)

| Condition | Why | Recommended Mode |
|-----------|-----|-----------------|
| Matching Skill + user has vault history | Overlay personalisation improves Skill accuracy | overlay |
| Security/audit/crypto task | High-risk — structured constraints prevent omissions | build |
| PRD or tech design submitted | Decomposition into structured prompt improves coverage | build |
| User says "thorough" / "comprehensive" / "production" | Explicit quality demand | build |
| Cross-domain task (e.g., "audit + deploy + monitor") | Multi-domain requires structured coordination | build |

### SHOULD Check (medium confidence — run hydrate preflight)

| Condition | Why |
|-----------|-----|
| Task > 200 chars description | Longer tasks tend to be more complex |
| Multiple technical keywords in one sentence | Domain overlap suggests hidden complexity |
| Task involves "migration" or "refactor" | Risk of breaking changes |
| User says "best practice" or "standard" | May benefit from vault-stored standards |
| First task in a new project | No vault history yet — build may create useful baseline |

### Should NOT Invoke

| Condition | Why |
|-----------|-----|
| Single-step CRUD | Direct execution is faster |
| Formatting / linting / renaming | No reasoning required |
| Trivial fixes (< 3 lines) | Overhead not justified |
| "What is X?" factual questions | Not a prompt engineering task |
| Tasks completable in < 3 trivial steps | SKILL.md `when_to_use` explicitly excludes |

## Mode Selection Logic

```
Hydrate preflight returns relevant entries:
  ├─ matched_skill is set → use mode="overlay"
  ├─ matched_skill is None + high-risk → use mode="build"
  └─ no relevant entries + low-risk → skip PromptCraft

After execution:
  → use mode="feedback" with execution result

When health report signals:
  → "[PC: N records, action=run_analysis]" → use mode="analyze"
  → "[PC: N records, action=review_evolution]" → use mode="advise"
  → "[PC: N records, action=review_creation]" → use mode="advise"
  → "[PC: N records, STALLED, action=stalled_needs_human]" → relay to user

When multiple independent tasks need processing:
  → use mode="batch" with an items array
  → Example: {"mode":"batch","items":[{"task":"audit token","skill_name":"solidity-audit"},{"task":"write docs"}]}
```

## Health Check at Session Start

At the start of a session, the main agent can proactively check PromptCraft health:

```bash
# Run hydrate.py --aggregate to get vault state
python skills/prompt-memory/scripts/hydrate.py --aggregate --min-records 10

# Or use the check_health() function programmatically
python -c "
import sys; sys.path.insert(0, 'promptcraft-agent')
from health_report import check_health
# Pass aggregate JSON from stdin
import json; data = json.loads(open(0).read())
h = check_health(data)
print(h.compact_str())
if h.recommended_action != 'none':
    print(f'Action needed: {h.recommended_action}')
    print(h.summary)
"
```

If `recommended_action` is not "none", the main agent should proactively inform
the user that PromptCraft has suggestions waiting.
