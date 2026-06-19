---
name: prompt-memory
description: >
  Workspace-anchored prompt memory management. This skill provides
  checkpoint.py (save prompt contexts to .promptcraft/prompt_vault.json with
  Git-style immutable version log) and hydrate.py (semantic search with
  keyword overlap scoring, version rollback, and version history listing).
  Use when the PromptCraft engine (promptcraft-agent/engine.py) or other tools
  need to persist or load prompt history. ALL persistence is file-based — no
  host memory API, no database.
---

# Prompt Memory Management

This skill manages the `.promptcraft/prompt_vault.json` workspace-anchored memory file
through two deterministic scripts. No host memory API is used — all state lives in the
project working directory as human-readable, editable JSON.

## Scripts

### `scripts/checkpoint.py` — Save a prompt checkpoint

Executed after the PromptCraft engine produces a prompt (via invoke_build, invoke_feedback,
or the feedback loop). Appends to the vault without overwriting previous versions.
Manages `version_tag`, `is_active`, and `parent_version` automatically.

When to call:
- After engine.invoke_build() produces a new prompt (new or versioned save).
- After engine.invoke_feedback() records execution results.
- After the feedback loop writes improvement notes — append feedback as a
  new version using `--version-of`, with `importance: "REFERENCE"`.

### `scripts/hydrate.py` — Load and filter prompt history

Executed at the start of a new PromptCraft engine session. Performs keyword-overlap semantic
search against the vault and returns only `is_active` versions of matching tasks.
By default returns compact results (metadata only, ~500 tokens). Use `--full` to
include the complete generated prompt text for reuse.

**Query Expansion (automatic):**

hydrate.py automatically expands queries with cross-language synonyms
(CJK→EN mapping) and compound-term detection before Jaccard search —
see `_QUERY_SYNONYMS` and `_expand_query()` in hydrate.py. No manual
query expansion needed.

**Query response structure:**

```json
{
  "status": "ok",
  "query": "<search text>",
  "auto_full_threshold": 0.75,
  "global_entries": [...],   // GLOBAL entries — always returned regardless of query match
  "results": [...],          // Top-k scored entries (excluding those already in global_entries)
  "total_active_tasks": 42
}
```

**`global_entries`** contains all active entries whose `summary.importance` is `"GLOBAL"`.
These represent cross-task long-term constraints and are returned unconditionally —
they ignore `--task-id` / `--skill` filters and are not subject to the top-k cutoff.
The caller MUST inject GLOBAL entries' `hard_constraints`, `summary.key_decisions`,
`summary.hard_constraints_added`, and `summary.summary_text` into every session context.

Each entry in both groups carries:
- `"global": true/false` — whether it came from the GLOBAL pool.
- `"auto_full": true/false` — whether the full prompt was auto-injected (score > threshold).

Also supports:
- `--rollback-to <v1>` to switch the active version for a task.
- `--list-versions` to inspect a task's full version chain.
- `--full` to include `generated_prompt` (complete text) in search results.

## Federation: Global Vault

PromptCraft supports a two-tier vault architecture. The **global vault**
(`~/.promptcraft/global_vault.json`) stores cross-project constraints and
shared prompt templates. The **project vault** (`.promptcraft/prompt_vault.json`)
stores project-specific history.

Hydrate.py automatically merges both vaults on every query — no extra flags
needed. Project entries take precedence when the same `task_id` exists in
both vaults.

### checkpoint.py `--global` flag

Save to the global vault instead of the project vault:

```bash
echo '{"task_id":"org-coding-standards","user_intent":"All Go code must use Gin + GORM"}' | \
  python checkpoint.py --global
```

### hydrate.py `--no-global` flag

Skip the global vault and search only the project vault:

```bash
python hydrate.py --query "audit contract" --no-global
```

## When to Use This Skill

Load `prompt-memory` alongside `promptcraft-agent/engine.py`. The scripts are
deterministic — execute them directly rather than loading them into the AI's context.

## Reference

- `references/vault-schema.md` — Full vault JSON structure with field descriptions
  including federation architecture and merge rules.
