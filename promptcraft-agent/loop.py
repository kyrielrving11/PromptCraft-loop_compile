"""PromptCraft Agent — Agent Loop orchestrator.

This is the top-level entry point. It ties the Engine (outer loop) and
Builder (single build) together into a coherent Agent invocation.

Cf. Claude Code's query() async generator — but PromptCraft's "loop" is not
a tight model→tool→model cycle. It is a cross-invocation cycle: the main
agent wakes PromptCraft multiple times across a task's lifecycle. This file
orchestrates each individual wake-up.

Usage:
    from loop import run_agent

    result = run_agent(request_json_string, hydrate_results_dict)
    # result is an AgentLoopResult — check .status to decide next action.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from protocol import (
    AgentLoopResult, AgentStatus, Mode, PromptCraftRequest, to_dict,
)
from engine import PromptCraftEngine
from health_report import compute_health


# ── Main entry point ──────────────────────────────────────────────────────────

def run_agent(
    request: PromptCraftRequest | str | dict[str, Any],
    hydrate_results: dict[str, Any] | None = None,
    engine: PromptCraftEngine | None = None,
) -> AgentLoopResult:
    """Execute one PromptCraft Agent invocation.

    Args:
        request: A PromptCraftRequest, a JSON string, or a dict.
        hydrate_results: Pre-loaded vault query results (from hydrate.py).
                         If None, the Agent operates without vault context.
        engine: An existing Engine instance (for session continuity).
                If None, a fresh Engine is created.

    Returns:
        AgentLoopResult — the main agent checks .status to decide:
          - OK      → execute the prompt, collect feedback, call back
          - STALLED → read the question, ask user, call back with answer
          - ERROR   → handle the failure
    """
    # ── Parse request ──
    if isinstance(request, str):
        try:
            data = json.loads(request)
        except json.JSONDecodeError:
            return AgentLoopResult(
                status=AgentStatus.ERROR,
                response=None,
            )
        request = PromptCraftRequest(**data)
    elif isinstance(request, dict):
        request = PromptCraftRequest(**request)

    # ── Initialise engine ──
    if engine is None:
        skills_dir = request.vault_config.skills_dir if request.vault_config else "skills"
        engine = PromptCraftEngine(skills_dir=skills_dir)

    # ── Route by mode ──
    # All modes (FULL, QUICK, REVIEW, FEEDBACK) flow through engine.invoke()
    return engine.invoke(request, hydrate_results)


# ── Sub-agent formatted output ─────────────────────────────────────────────

def format_subagent_output(
    result: AgentLoopResult,
    engine: PromptCraftEngine | None = None,
    mode: str = "",
) -> str:
    """Format AgentLoopResult for sub-agent LLM consumption.

    Unlike the raw JSON output (which exposes all engine internals), this
    produces a compact structured summary — the sub-agent LLM relays this
    to the main agent without exposing vault internals.

    Returns a JSON string with keys: health, status, mode, prompt_or_overlay,
    analysis, technique_used, confidence.
    """
    # Compute health from engine state if available
    if engine is not None and engine.state is not None:
        health = compute_health(
            buffer_size=len(engine.state.feedback_buffer),
            quality_trend=engine.state.quality_trend,
            analysis_count=engine.state.analysis_count,
        )
    else:
        health = compute_health(0, [])

    # Extract payload from result
    prompt_or_overlay: str | None = None
    analysis: dict[str, Any] | None = None
    technique_used: str | None = None
    confidence: float = 0.0

    if result.stalled is not None:
        effective_mode = "stalled"
        prompt_or_overlay = result.stalled.question_for_main_agent
        confidence = 0.3
    elif result.feedback is not None:
        fb = result.feedback
        effective_mode = "feedback"
        prompt_or_overlay = f"Quality: {fb.quality_score}/5. {fb.improvement_notes}"
        confidence = min(fb.quality_score / 5.0, 1.0)
    elif mode:
        effective_mode = mode
    elif result.response is not None:
        r = result.response
        prompt_or_overlay = r.prompt
        if r.analysis:
            analysis = to_dict(r.analysis)
            technique_used = r.analysis.technique if hasattr(r.analysis, "technique") else None
        confidence = 0.8
        effective_mode = "build"
    else:
        effective_mode = "unknown"

    # Always extract response prompt as fallback
    if prompt_or_overlay is None and result.response is not None:
        prompt_or_overlay = result.response.prompt
        if not analysis and result.response.analysis:
            analysis = to_dict(result.response.analysis)
            technique_used = result.response.analysis.technique if hasattr(result.response.analysis, "technique") else None
        if confidence == 0.0:
            confidence = 0.8

    output = {
        "health": health.compact_line(),
        "status": result.status.value,
        "mode": effective_mode,
        "prompt_or_overlay": prompt_or_overlay,
        "analysis": analysis,
        "technique_used": technique_used,
        "confidence": confidence,
    }

    return json.dumps(output, indent=2, ensure_ascii=False)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    """CLI for testing the Agent Loop.

    Reads a JSON PromptCraftRequest from stdin, prints the AgentLoopResult to stdout.

    Usage:
        echo '{"task":"audit smart contract","mode":"full"}' | python loop.py
        python loop.py --task "build a REST API" --mode quick
        python loop.py --task "audit contract" --mode overlay --skill-name solidity-audit
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="PromptCraft Agent — prompt engineering sub-agent.",
    )
    parser.add_argument(
        "--task", "-t",
        help="Task description (overrides stdin).",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["full", "quick", "review", "feedback", "overlay", "analyze", "advise"],
        default="full",
        help="Agent invocation mode (overlay = Skill personalisation).",
    )
    parser.add_argument(
        "--skill-name",
        default=None,
        help="Skill name for overlay mode.",
    )
    parser.add_argument(
        "--skills-dir",
        default="skills",
        help="Path to skills directory (for vault scripts and technique refs).",
    )
    parser.add_argument(
        "--tech-stack",
        default="",
        help="Known tech stack for the task.",
    )
    parser.add_argument(
        "--prd",
        default="",
        help="Path to PRD file or PRD text.",
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        help="Read request JSON from file instead of stdin.",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["json", "subagent"],
        default="json",
        help="Output format: json (raw engine output, for tests) or subagent (compact, for sub-agent LLM).",
    )
    args = parser.parse_args()

    # ── Build request ──
    if args.task:
        from protocol import Context
        request = PromptCraftRequest(
            task=args.task,
            mode=Mode(args.mode),
            skill_name=args.skill_name,
            context=Context(tech_stack=args.tech_stack, prd=args.prd) if (args.tech_stack or args.prd) else Context(),
        )
    elif args.input:
        raw = args.input.read_text(encoding="utf-8")
        data = json.loads(raw)
        request = PromptCraftRequest(**data)
    else:
        # Read from stdin
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({"status": "error", "error": "No input provided."}, ensure_ascii=False))
            sys.exit(1)
        data = json.loads(raw)
        request = PromptCraftRequest(**data)

    # ── Execute ──
    engine = PromptCraftEngine(skills_dir=args.skills_dir)
    result = run_agent(request, engine=engine)

    # ── Output ──
    sys.stdout.reconfigure(encoding="utf-8")
    if args.format == "subagent":
        print(format_subagent_output(result, engine, args.mode))
    else:
        output = to_dict(result)
        print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
