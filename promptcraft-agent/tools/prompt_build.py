"""PromptCraft Agent — Prompt Build Tool.

The fallback pipeline: when no Skill matches the task, this tool runs
the complete prompt-engineering workflow:

  hydrate → route → build 8-section → checkpoint → return

It wraps the existing builder.py pipeline — all technique selection and
section assembly logic lives there. This tool is the "兜底" (safety net)
for tasks without pre-existing Skills.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult, tool_ok, tool_error

# Ensure promptcraft-agent/ is on sys.path so we can import builder unconditionally.
_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from builder import build  # noqa: E402


class PromptBuildTool(Tool):
    """Full prompt-engineering pipeline for tasks without a matching Skill."""

    name = "prompt_build"
    description = "Generate a structured 8-section prompt when no Skill exists."

    # Safety: reads skills (for technique refs), writes to vault (checkpoint)
    WRITES_TO_VAULT = True
    READS_SKILLS = True

    def check_permissions(self, input: dict[str, Any], context: Any = None) -> Any:
        from protocol import tool_permission_allow
        task = input.get("task", "")
        if not task or len(str(task).strip()) < 3:
            from protocol import tool_permission_deny
            return tool_permission_deny("Task too short for prompt build.")
        # PromptBuild never modifies Skill files — the hard boundary holds
        return tool_permission_allow()

    def is_applicable(self, request: Any, context: dict[str, Any] | None = None) -> bool:
        # This is the fallback — it applies when no more-specific tool has claimed
        # the request. The registry checks tools in priority order; this one runs
        # last (registered last).
        return True

    def call(self, request: Any, context: Any = None) -> ToolResult:
        hydrate_results = context.hydrate_results if context else None

        try:
            result = build(request, hydrate_results)
        except Exception as exc:
            return tool_error(f"PromptBuildTool failed: {exc}")

        return tool_ok(
            prompt=result.response.prompt,
            analysis={
                "technique": result.response.analysis.technique if result.response.analysis else "",
                "rationale": result.response.analysis.rationale if result.response.analysis else "",
            },
            metadata={
                "task_id": result.response.metadata.task_id if result.response.metadata else "",
                "technique": result.technique,
                "hard_constraints": result.hard_constraints,
                "key_decisions": result.key_decisions,
            },
        )

    def prompt(self) -> str:
        return (
            "- **Prompt Build**: When no existing Skill covers the user's task, "
            "use this tool to generate a structured prompt from scratch. "
            "It analyses the task, selects the best prompt-engineering technique, "
            "and returns a complete 8-section prompt ready for execution."
        )
