"""PromptCraft Agent — Feedback Collect Tool.

Captures both explicit feedback (user says "missing X") and implicit
signals (user follows up, edits the prompt, skips a Skill) and stores
them as structured vault entries.

Principle: behaviour IS feedback. Not just ratings.
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolResult, tool_ok, tool_error


class FeedbackCollectTool(Tool):
    """Collect explicit + implicit feedback signals and persist to vault."""

    name = "feedback_collect"
    description = "Record execution feedback (explicit + implicit) into vault."

    # Safety: writes feedback to vault only
    WRITES_TO_VAULT = True

    # Signal type constants
    EXPLICIT = "explicit"
    IMPLICIT_FOLLOWUP = "implicit_followup"
    IMPLICIT_EDIT = "implicit_edit"
    IMPLICIT_SKIP = "implicit_skip"

    def is_applicable(self, request: Any, context: Any = None) -> bool:
        # Triggered by feedback mode OR when signals are present in context
        if getattr(request, "mode", None) == "feedback":
            return True
        signals = context.feedback_signals if context else []
        return len(signals) > 0

    def call(self, request: Any, context: Any = None) -> ToolResult:
        signals: list[dict[str, Any]] = []

        # ── Explicit feedback via Feedback mode ──
        if getattr(request, "mode", None) == "feedback" and request.feedback:
            signals.append(self._from_feedback(request))

        # ── Implicit signals from context ──
        ctx_signals = context.feedback_signals if context else []
        for sig in ctx_signals:
            if isinstance(sig, dict):
                signals.append(sig)

        if not signals:
            return tool_error("No feedback signals to collect.")

        return tool_ok(
            signals=signals,
            count=len(signals),
            vault_payload={
                "task_type": request.task[:80] if request.task else "",
                "skill_used": getattr(request, "skill_name", None),
                "signals": signals,
            },
        )

    def _from_feedback(self, request: Any) -> dict[str, Any]:
        fb = request.feedback
        # Handle both dataclass (attribute) and dict (key) access
        success = getattr(fb, "success", None) if hasattr(fb, "success") else (
            fb.get("success") if isinstance(fb, dict) else None
        )
        violations = getattr(fb, "constraint_violations", None) if hasattr(fb, "constraint_violations") else (
            fb.get("constraint_violations", []) if isinstance(fb, dict) else []
        )
        fixes = getattr(fb, "manual_fixes_needed", None) if hasattr(fb, "manual_fixes_needed") else (
            fb.get("manual_fixes_needed", "") if isinstance(fb, dict) else ""
        )
        return {
            "signal_type": self.EXPLICIT,
            "description": (
                f"success={success}, violations={violations}, "
                f"fixes={fixes}"
            ),
            "task_type": request.task[:80] if request.task else "",
            "skill_used": getattr(request, "skill_name", None),
            "overlay_used": getattr(request, "overlay_used", []),
        }

    def check_permissions(self, input: dict[str, Any], context: Any = None) -> Any:
        from protocol import tool_permission_allow, tool_permission_deny
        fb = input.get("feedback")
        if fb is None and not (context and getattr(context, "feedback_signals", None)):
            return tool_permission_deny("No feedback data to collect.")
        # Quality score validation: must be 1-5 or unset (0)
        score = input.get("quality_score", 0)
        if isinstance(score, (int, float)) and score not in (0, 1, 2, 3, 4, 5):
            return tool_permission_deny(f"Invalid quality score: {score}. Must be 1-5.")
        return tool_permission_allow()

    def prompt(self) -> str:
        return (
            "- **Feedback Collect**: After a Skill or prompt executes, "
            "capture the outcome. Explicit feedback (user says 'missing X') "
            "and implicit signals (user follows up, edits the prompt) are "
            "both valid. Behaviour IS feedback."
        )
