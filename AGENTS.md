@CLAUDE.md

## Agent-specific notes

- **Sub-agent `promptcraft`** is available for prompt engineering tasks —
  use `Agent(subagent_type="promptcraft", ...)` or let `promptcraft-bridge`
  skill auto-trigger it for complex tasks.
- **Vault** is at `.promptcraft/` (project) + `~/.promptcraft/` (global).
- **Verify** with: `python -m unittest discover -s tests -p "test_*.py"`
