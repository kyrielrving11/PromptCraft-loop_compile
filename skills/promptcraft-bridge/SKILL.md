---
name: promptcraft-bridge
description: >
  Trigger for PromptCraft sub-agent. Use when task is complex, high-risk,
  multi-step, or cross-domain. This skill does nothing except delegate to
  the promptcraft sub-agent.
when_to_use: >
  TRIGGER when: user task involves security/audit/crypto/deploy/compliance,
  OR requires multi-step reasoning across domains,
  OR a PRD or technical design document is submitted,
  OR a matched Skill would benefit from vault constraint personalisation,
  OR a prompt has just been executed and feedback should be recorded,
  OR a PromptCraft health report recommends action (run_analysis /
     review_evolution / review_creation).
  DO NOT TRIGGER when: simple single-step tasks, formatting, basic CRUD,
  trivial renames, or tasks completable in < 3 trivial steps.
user-invocable: true
allowed-tools:
  - Bash(python *)
  - Read
---

# PromptCraft Bridge

Trigger-only wrapper. Delegates all work to the PromptCraft sub-agent.
Full mode documentation lives in `.claude/agents/promptcraft.md`.

## Workflow

The bridge decides whether to invoke PromptCraft. No assess sub-agent call —
the `when_to_use` heuristics + a cheap vault hydrate are the gate.

```bash
# 1. Quick hydrate to check for relevant vault history (~50ms, no LLM cost)
python skills/prompt-memory/scripts/hydrate.py --query "<task>" --top 3

# 2. If hydrate returns relevant entries (Jaccard ≥ 0.3 on any result):
#    → invoke overlay (has Skill) or build (no Skill) directly
echo '{"task":"<task>","mode":"<overlay|build>","skill_name":"<if known>"}' \
  | python promptcraft-agent/subagent_adapter.py

# 3. If no relevant entries AND task is high-risk (security/audit/crypto/deploy/migrat):
#    → invoke build (establishes vault baseline)
echo '{"task":"<task>","mode":"build"}' \
  | python promptcraft-agent/subagent_adapter.py

# 4. Otherwise → skip PromptCraft, execute directly

# 5. After execution, record feedback
echo '{"task":"<task>","mode":"feedback","feedback":{...}}' \
  | python promptcraft-agent/subagent_adapter.py

# 6. Batch multiple tasks
echo '{"mode":"batch","items":[{"task":"...","skill_name":"..."},{"task":"..."}]}' \
  | python promptcraft-agent/subagent_adapter.py
```

## Health Signals

The health report line in every response tells you what to do next:

| Signal | Action |
|--------|--------|
| `[PC: N records, normal]` | Continue |
| `[PC: N records, action=run_analysis]` | Call `mode=analyze` |
| `[PC: N records, action=review_evolution]` | Call `mode=advise`, present suggestions to user |
| `[PC: N records, STALLED, ...]` | Relay to user — circuit breaker tripped |
| `status=error` with denial reason | Input/output guard blocked the request — check the error message |
