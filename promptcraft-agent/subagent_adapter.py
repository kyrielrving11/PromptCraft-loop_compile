"""PromptCraft Agent — Sub-agent adapter (unified entry point).

This is the single entry point when PromptCraft is invoked as a Claude Code
sub-agent via `Agent(subagent_type="promptcraft", ...)`. It wraps the Engine,
routes by mode, and always prepends a compact HealthReport.

Design (from subagent-orchestration-plan.md):
    - Sub-agent is a pure function wrapper — stateless per call, persistence via vault
    - Internal process is invisible — only the final result is returned
    - Health Report is the only signal mechanism

Five modes:
    overlay  — Skill personalisation (skill_name must be set)
    build    — Full 8-section prompt generation (fallback, no matching Skill)
    feedback — Record execution results
    analyze  — Pattern analysis on accumulated feedback
    advise   — Skill evolution/creation suggestions

Usage:
    from subagent_adapter import handle

    result_json = handle('{"task":"...","mode":"build"}')
    # Returns compact JSON with health report header + result body.
"""

from __future__ import annotations

import json
from typing import Any

from protocol import (
    AgentLoopResult, AgentStatus, Mode, PromptCraftRequest, to_dict,
)
from engine import PromptCraftEngine, create_engine
from boundary import guard_input, guard_output


# ── Mode mapping (sub-agent mode → engine Mode) ────────────────────────────────

# Sub-agent "build" maps to engine Mode.FULL (full pipeline)
MODE_MAP: dict[str, Mode] = {
    "overlay":  Mode.OVERLAY,
    "build":    Mode.FULL,
    "feedback": Mode.FEEDBACK,
    "analyze":  Mode.ANALYZE,
    "advise":   Mode.ADVISE,
    # Legacy modes pass through directly
    "full":     Mode.FULL,
    "quick":    Mode.QUICK,
    "review":   Mode.REVIEW,
    # Phase 5: batch processing
    "batch":    Mode.BATCH,
}


# ── Main entry point ────────────────────────────────────────────────────────────

def handle(
    request_input: str | dict[str, Any] | PromptCraftRequest,
    engine: PromptCraftEngine | None = None,
) -> str:
    """Single entry point for sub-agent invocation.

    Parses the request, routes by mode to the Engine, computes health,
    and returns a compact JSON result string.

    Args:
        request_input: JSON string, dict, or PromptCraftRequest.
        engine: Existing Engine instance (for session continuity).
                If None, a fresh Engine is created.

    Returns:
        Compact JSON string with keys: "status", "health", "result".
    """
    # ── Parse request (capture raw mode before mapping) ──
    # BATCH: detect early — different input contract (items array, not task)
    if isinstance(request_input, dict):
        raw_mode = request_input.get("mode", "full")
        raw_data = request_input
    elif isinstance(request_input, str):
        raw_data = json.loads(request_input)
        raw_mode = raw_data.get("mode", "full")
    elif isinstance(request_input, PromptCraftRequest):
        raw_mode = request_input.mode.value if isinstance(request_input.mode, Mode) else str(request_input.mode)
        raw_data = None
        request = request_input
    else:
        raise TypeError(f"Expected str, dict, or PromptCraftRequest; got {type(request_input).__name__}")

    # ── BATCH: early return — different input/output contract ──
    if raw_mode == "batch" and raw_data is not None:
        from protocol import BatchRequest, BatchItem
        items_data = raw_data.get("items", []) if isinstance(raw_data, dict) else []
        from boundary import guard_batch_input
        batch_guard = guard_batch_input(items_data)
        if not batch_guard.ok:
            return json.dumps({
                "health": "[PromptCraft] records=0 quality=0.0",
                "status": "error",
                "result": {"mode": "batch", "error": batch_guard.reason},
            }, indent=2, ensure_ascii=False)

        batch_req = BatchRequest(
            items=[BatchItem(**item) for item in items_data] if items_data else [],
            task_id=raw_data.get("task_id") if isinstance(raw_data, dict) else None,
        )
        if engine is None:
            engine = create_engine()
        batch_response = engine.invoke_batch(batch_req)
        loop_result = engine._batch_response_to_loop(batch_response)
        health = engine.maybe_silent_analyze()
        return _build_agent_response(loop_result, health, "batch")

    # ── Non-batch: parse into PromptCraftRequest ──
    if raw_data is not None:
        request = _parse_request(raw_data)

    # ── Normalise mode for engine ──
    engine_mode = MODE_MAP.get(raw_mode, Mode.FULL)
    if raw_data is not None:
        request.mode = engine_mode

    # ── Layer 1: Input boundary ──
    input_guard = guard_input(
        task=request.task or "",
        mode=raw_mode,
        skill_name=getattr(request, "skill_name", None),
        feedback_present=request.feedback is not None,
    )
    if not input_guard.ok:
        return json.dumps({
            "health": "[PromptCraft] records=0 quality=0.0",
            "status": "error",
            "result": {"mode": raw_mode, "error": input_guard.reason},
        }, indent=2, ensure_ascii=False)

    # ── Initialise engine ──
    if engine is None:
        skills_dir = request.vault_config.skills_dir if request.vault_config else "skills"
        engine = create_engine(skills_dir=skills_dir)

    # ── Execute via dedicated engine method ──
    result = _route_to_engine(engine, request)

    # ── Silent analysis after every mode (returns HealthReport) ──
    health = engine.maybe_silent_analyze()

    # ── Build and return response ──
    return _build_agent_response(result, health, raw_mode)


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _route_to_engine(
    engine: PromptCraftEngine,
    request: PromptCraftRequest,
) -> AgentLoopResult:
    """Route request to the appropriate dedicated engine method.

    Uses the 6 public invoke_* methods. Falls back to engine.invoke()
    for legacy modes (REVIEW) or unknown modes.
    """
    mode = request.mode

    if mode == Mode.OVERLAY:
        return engine.invoke_overlay(request)

    if mode == Mode.ANALYZE:
        return engine.invoke_analyze(request)

    if mode == Mode.ADVISE:
        return engine.invoke_advise(request)

    if mode == Mode.FEEDBACK:
        return engine.invoke_feedback(request)

    if mode in (Mode.FULL, Mode.QUICK):
        return engine.invoke_build(request)

    # Legacy / unknown: fall back to generic invoke()
    return engine.invoke(request)


def _parse_request(
    raw: str | dict[str, Any] | PromptCraftRequest,
) -> PromptCraftRequest:
    """Parse a request from any accepted input format."""
    if isinstance(raw, PromptCraftRequest):
        return raw

    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON request: {exc}") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise TypeError(f"Expected str, dict, or PromptCraftRequest; got {type(raw).__name__}")

    # Normalise mode string → Mode enum (mapping happens in handle())
    mode = data.get("mode", "full")
    if isinstance(mode, str):
        valid_modes = {m.value for m in Mode}
        data["mode"] = Mode(mode) if mode in valid_modes else Mode.FULL
    elif not isinstance(mode, Mode):
        data["mode"] = Mode.FULL

    return PromptCraftRequest(**data)


def _build_agent_response(
    result: AgentLoopResult,
    health: Any,  # HealthReport
    mode: str = "",
) -> str:
    """Build the compact JSON response the main agent sees.

    Uses SubagentOutput as the unified schema. The main agent reads:
      - health.compact_line() — one-line status signal
      - mode — which mode produced this output
      - prompt_or_overlay — the actual payload (prompt, overlay, or None)
      - analysis — PatternReport or SkillAdvice (only for analyze/advise modes)
      - technique_used — which technique was selected (build mode)
      - confidence — 0-1 confidence score
    """
    # Extract prompt text from whichever result field is populated
    prompt_or_overlay: str | None = None
    analysis: dict[str, Any] | None = None
    technique_used: str | None = None
    confidence: float = 0.0

    if result.response is not None:
        r = result.response
        prompt_or_overlay = r.prompt
        if r.analysis:
            analysis = to_dict(r.analysis)
            technique_used = r.analysis.technique if hasattr(r.analysis, "technique") else None

    if result.feedback is not None:
        fb = result.feedback
        prompt_or_overlay = (
            f"Quality: {fb.quality_score}/5. {fb.improvement_notes}"
            if fb.improvement_notes else f"Quality: {fb.quality_score}/5."
        )

    if result.stalled is not None:
        prompt_or_overlay = result.stalled.question_for_main_agent
        confidence = 0.3  # Low confidence when stalled

    # Determine effective mode
    effective_mode = mode or (
        "stalled" if result.status.value == "stalled" else "unknown"
    )

    # Build SubagentOutput
    payload = {
        "mode": effective_mode,
        "prompt_or_overlay": prompt_or_overlay,
        "analysis": analysis,
        "technique_used": technique_used,
        "confidence": confidence,
        "proactive_signals": (
            health.proactive_signals
            if hasattr(health, 'proactive_signals') and health.proactive_signals
            else []
        ),
    }

    # ── Layer 4: Output boundary ──
    output_guard = guard_output(
        payload,
        health_report=health.compact_line() if hasattr(health, 'compact_line') else str(health),
    )
    health_line = health.compact_line() if hasattr(health, 'compact_line') else str(health)
    if output_guard.warnings:
        health_line += " [warn: " + "; ".join(output_guard.warnings) + "]"

    output = {
        "health": health_line,
        "status": result.status.value,
        "result": payload,
    }

    return json.dumps(output, indent=2, ensure_ascii=False)


# ── CLI entry point (for direct testing) ────────────────────────────────────────

def main() -> None:
    """CLI entry point for testing the sub-agent adapter.

    Reads JSON from stdin, writes result to stdout.

    Usage:
        echo '{"task":"audit contract","mode":"build"}' | python subagent_adapter.py
    """
    import sys

    raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({"status": "error", "error": "No input provided."}, ensure_ascii=False))
        sys.exit(1)

    try:
        output = handle(raw)
        sys.stdout.reconfigure(encoding="utf-8")
        print(output)
    except Exception as exc:
        print(json.dumps({
            "health": "[PromptCraft] records=0",
            "status": "error",
            "result": {"error": str(exc)},
        }, indent=2, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
