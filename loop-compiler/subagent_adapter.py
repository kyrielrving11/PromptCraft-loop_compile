"""PromptCraft-loop_compile — Sub-agent adapter (unified entry point).

This is the single entry point when PromptCraft is invoked as a Claude Code
sub-agent via `Agent(subagent_type="promptcraft", ...)`. It wraps the Engine,
routes by mode, and always prepends a compact HealthReport.

Design (from subagent-orchestration-plan.md):
    - Sub-agent is a pure function wrapper — stateless per call, persistence via vault
    - Internal process is invisible — only the final result is returned
    - Health Report is the only signal mechanism

Three modes (v3.4):
    loop_compile — Per-iteration prompt compiler (primary entry point)
    feedback     — Record execution results → quality scoring → vault persistence
    review       — Audit prompt quality (structural checks + constraint compliance)

build is an internal path (loop_compile L2 delegation) — not an exposed mode.

Usage:
    from subagent_adapter import handle

    result_json = handle('{"task":"...","mode":"loop_compile","loop_id":"t","round":1}')
    # Returns compact JSON with health report header + result body.
"""

from __future__ import annotations

import json
from typing import Any

from protocol import (
    AgentLoopResult, AgentStatus, Mode, PromptCraftRequest, to_dict,
)
from engine import PromptCraftEngine, create_engine


# ── Mode mapping (v3.4: 3 external + 1 internal) ──────────────────────────────

MODE_MAP: dict[str, Mode] = {
    "loop_compile": Mode.LOOP_COMPILE,
    "feedback":     Mode.FEEDBACK,
    "review":       Mode.REVIEW,
    "build":        Mode.BUILD,       # Internal: loop_compile L2 delegation
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

    # ── Non-batch: parse into PromptCraftRequest ──
    if raw_data is not None:
        request = _parse_request(raw_data)

    # ── Normalise mode for engine ──
    engine_mode = MODE_MAP.get(raw_mode)
    if engine_mode is None:
        return json.dumps({
            "health": "[PC: 0 records, normal]",
            "status": "error",
            "result": {"mode": raw_mode, "error": f"Unknown mode: {raw_mode}"},
        }, indent=2, ensure_ascii=False)
    if raw_data is not None:
        request.mode = engine_mode

    # ── Inline input validation (v3.4: replaces boundary.guard_input) ──
    task = getattr(request, 'task', '') or ''
    if not task.strip():
        return json.dumps({
            "health": "[PC: 0 records, normal]",
            "status": "error",
            "result": {"mode": raw_mode, "error": "Task is required."},
        }, indent=2, ensure_ascii=False)

    # ── Initialise engine ──
    if engine is None:
        skills_dir = request.vault_config.skills_dir if request.vault_config else "skills"
        engine = create_engine(skills_dir=skills_dir)

    # ── Execute via dedicated engine method ──
    result = _route_to_engine(engine, request)

    # ── Build compact health line (v3.4: replaces HealthReport) ──
    health_line = _compact_health(engine)

    # ── Build and return response ──
    return _build_agent_response(result, health_line, raw_mode)


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _route_to_engine(
    engine: PromptCraftEngine,
    request: PromptCraftRequest,
) -> AgentLoopResult:
    """Route request to the appropriate engine method (v3.4: 3-mode + build)."""
    mode = request.mode

    if mode == Mode.LOOP_COMPILE:
        return engine.invoke_loop_compile(request)
    if mode == Mode.FEEDBACK:
        return engine.invoke_feedback(request)
    if mode == Mode.REVIEW:
        return engine._handle_review(request, None)
    if mode == Mode.BUILD:
        return engine.invoke_build(request)

    return AgentLoopResult(
        status=AgentStatus.ERROR,
        response=None,
    )


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
    mode = data.get("mode", "build")
    if isinstance(mode, str):
        valid_modes = {m.value for m in Mode}
        data["mode"] = Mode(mode) if mode in valid_modes else Mode.BUILD
    elif not isinstance(mode, Mode):
        data["mode"] = Mode.BUILD

    # Filter to known PromptCraftRequest fields, attach extras as attributes.
    # loop_compile mode sends fields (loop_id, round, goal_id, etc.) that
    # PromptCraftRequest doesn't accept — these are read by engine via getattr().
    from dataclasses import fields as dc_fields
    known = {f.name for f in dc_fields(PromptCraftRequest)}
    filtered = {k: v for k, v in data.items() if k in known}
    extras = {k: v for k, v in data.items() if k not in known}

    req = PromptCraftRequest(**filtered)
    for k, v in extras.items():
        setattr(req, k, v)
    return req


def _compact_health(engine: PromptCraftEngine) -> str:
    """Build compact health line with silent-failure metrics when degraded.

    Format: [PC: N records, normal] or [PC: N records, STALLED, write_err=3]
    Appends error counters only when non-zero — keeps normal lines short.
    """
    record_count = len(engine.state.quality_trend) if engine.state else 0
    stalled = engine._should_break() if engine.state else False
    status = "STALLED" if stalled else "normal"

    parts = [f"PC: {record_count} records", status]

    # Surface silent-failure counters when non-zero (v3.5.2)
    m = engine._metrics
    if m:
        if m.vault_write_errors:
            parts.append(f"write_err={m.vault_write_errors}")
        if m.vault_write_timeouts:
            parts.append(f"write_timeout={m.vault_write_timeouts}")
        if m.subprocess_timeouts:
            parts.append(f"sub_timeout={m.subprocess_timeouts}")
        if m.hydrate_cache_misses:
            parts.append(f"cache_miss={m.hydrate_cache_misses}")

    return "[" + ", ".join(parts) + "]"


def _build_agent_response(
    result: AgentLoopResult,
    health_line: str,
    mode: str = "",
) -> str:
    """Build the compact JSON response the main agent sees (v3.4)."""
    prompt_or_overlay: str | None = None
    analysis: dict[str, Any] | None = None
    technique_used: str | None = None

    if result.response is not None:
        r = result.response
        prompt_or_overlay = r.prompt
        if r.analysis:
            analysis = to_dict(r.analysis)
            technique_used = r.analysis.technique if hasattr(r.analysis, "technique") else None

    effective_mode = mode or "unknown"

    # Inline output size guard (v3.4: replaces boundary.guard_output)
    prompt_text = prompt_or_overlay or ""
    if len(prompt_text) > 32_000:
        prompt_text = prompt_text[:32_000] + "\n\n[truncated — exceeds 32KB]"

    payload = {
        "mode": effective_mode,
        "prompt_or_overlay": prompt_text,
        "analysis": analysis,
        "technique_used": technique_used,
        "confidence": 0.0,
        "proactive_signals": [],
    }

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
