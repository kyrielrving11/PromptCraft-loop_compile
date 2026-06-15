---
name: prompt-memory
description: >
  Workspace-anchored prompt memory management. This skill provides
  checkpoint.py (save prompt contexts to .promptcraft/prompt_vault.json with
  Git-style immutable version log) and hydrate.py (semantic search with
  keyword overlap scoring, version rollback, and version history listing).
  Use when prompt-craft or prompt-review needs to persist or load prompt
  history. ALL persistence is file-based — no host memory API, no database.
---

# Prompt Memory Management

This skill manages the `.promptcraft/prompt_vault.json` workspace-anchored memory file
through two deterministic scripts. No host memory API is used — all state lives in the
project working directory as human-readable, editable JSON.

## Scripts

### `scripts/checkpoint.py` — Save a prompt checkpoint

Executed after prompt-craft or prompt-review produces a prompt. Appends to the vault
without overwriting previous versions. Manages `version_tag`, `is_active`, and
`parent_version` automatically.

When to call:
- After Step 4 of prompt-craft's workflow (new or versioned save).
- After prompt-review produces an improved version.

### `scripts/hydrate.py` — Load and filter prompt history

Executed at the start of a new prompt-craft session. Performs keyword-overlap semantic
search against the vault and returns only `is_active` versions of matching tasks.
By default returns compact results (metadata only, ~500 tokens). Use `--full` to
include the complete generated prompt text for reuse.

Also supports:
- `--rollback-to <v1>` to switch the active version for a task.
- `--list-versions` to inspect a task's full version chain.
- `--full` to include `generated_prompt` (complete text) in search results.

## When to Use This Skill

Load `prompt-memory` alongside `prompt-craft` or `prompt-review`. The scripts are
deterministic — execute them directly rather than loading them into the AI's context.

## Reference

- `references/vault-schema.md` — Full vault JSON structure with field descriptions.
