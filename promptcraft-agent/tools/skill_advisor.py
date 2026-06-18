"""PromptCraft Agent — Skill Advisor Tool.

Produces Skill evolution or creation suggestions based on PatternAnalysis
results. Does NOT generate SKILL.md itself — that is the main agent's
built-in capability (e.g. Claude Code /create-skill). PromptCraft only
provides the evidence and draft content; the main agent executes.

Principle: PromptCraft analyses, main agent acts.
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolResult, tool_ok, tool_error


class SkillAdvisorTool(Tool):
    """Generate Skill improvement suggestions backed by execution data."""

    name = "skill_advisor"
    description = "Suggest Skill evolution or creation based on pattern analysis."

    # Safety: reads skills for context, writes suggestions to vault
    # MODIFIES_SKILLS is False (default) — HARD DENY, bypass-immune.
    # This tool only suggests; the main agent executes via /create-skill.
    READS_SKILLS = True
    WRITES_TO_VAULT = True

    def check_permissions(self, input: dict[str, Any], context: Any = None) -> Any:
        from protocol import tool_permission_allow, tool_permission_deny, tool_permission_warn
        # HARD DENY: never auto-modify Skill files
        if self.MODIFIES_SKILLS:
            return tool_permission_deny(
                "Skill modification is bypass-immune — PromptCraft never "
                "auto-modifies Skill files. Return suggestions to main agent instead."
            )
        # Warn if no pattern_report
        report = context.pattern_report if context else None
        if report is None:
            return tool_permission_warn(
                "No pattern_report in context — advice may be generic."
            )
        return tool_permission_allow()

    def is_applicable(self, request: Any, context: Any = None) -> bool:
        # Triggered when pattern analysis results are available
        report = context.pattern_report if context else None
        return report is not None

    def call(self, request: Any, context: Any = None) -> ToolResult:
        report = context.pattern_report if context else None
        if report is None:
            return tool_error("SkillAdvisor requires a pattern_report in context.")

        # ── Normalise: accept both dict and PatternReport dataclass ──
        if hasattr(report, "__dataclass_fields__"):
            from dataclasses import asdict
            report = asdict(report)

        advice_list: list[dict[str, Any]] = []

        # ── Evolution advice: high-freq overlays → suggest Skill upgrade ──
        for item in report.get("high_freq_overlays", []):
            overlay = item.get("overlay", "")
            pct = item.get("pct", 0)
            total = report.get("total_executions", 0)
            count = item.get("count", 0)
            advice_list.append({
                "advice_type": "evolution",
                "suggestion": (
                    f"Consider adding '{overlay}' to the Skill's default "
                    f"instructions — {pct}% of users add this manually."
                ),
                "data_support": f"{count} out of {total} executions.",
                "draft_content": (
                    f"## {overlay}\n\n"
                    f"Always check for {overlay.lower()} during the audit. "
                    f"[Auto-suggested by PromptCraft — {pct}% of users add this constraint.]"
                ),
            })

        # ── Creation advice: high-frequency task type with no Skill ──
        for task_type in report.get("low_quality_task_types", []):
            advice_list.append({
                "advice_type": "creation",
                "suggestion": (
                    f"Tasks of type '{task_type}' have consistently low quality. "
                    "Consider creating a dedicated Skill for this domain."
                ),
                "data_support": (
                    f"Average quality score < 3 across multiple executions. "
                    "A domain Skill with built-in constraints may improve this."
                ),
                "draft_content": (
                    f"# {task_type.replace('_', ' ').title()} Skill\n\n"
                    f"## Purpose\nAutomates {task_type} tasks with validated best practices.\n\n"
                    f"## Key Constraints\n[Auto-generated draft — review before use]\n"
                ),
            })

        if not advice_list:
            return tool_ok(advice=[], note="No actionable advice from current data.")

        return tool_ok(advice=advice_list, count=len(advice_list))

    def prompt(self) -> str:
        return (
            "- **Skill Advisor**: When pattern analysis finds opportunities, "
            "call this tool to generate Skill evolution or creation suggestions. "
            "The suggestions include evidence and draft content — pass them to "
            "the main agent's built-in Skill creation mechanism (e.g. /create-skill). "
            "Never auto-modify Skills. Always wait for user confirmation."
        )
