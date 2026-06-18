"""PromptCraft Agent — Single prompt build pipeline.

promptBuilder is a pure-function pipeline. One invocation = one complete build cycle.
It receives a hydrated Request (with vault results already loaded) and returns a
complete PromptCraftResponse.

It is stateless per call — all iteration state lives in the Engine (engine.py).

Cf. Claude Code's query() — but linear, not a loop. The "loop" in PromptCraft
happens across invocations, managed by the Engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from protocol import (
    AgentStatus, Analysis, CognitiveLoad,
    DomainKnowledge, Importance, Independence, Metadata,
    Mode, PromptCraftRequest, PromptCraftResponse, Summary,
    Technique, VaultRef, make_task_id, new_vault_ref,
)


# ── Builder result (internal) ─────────────────────────────────────────────────

@dataclass
class BuildResult:
    """What promptBuilder returns to the Engine."""
    response: PromptCraftResponse
    technique: str
    importance: str
    hard_constraints: list[str] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)


# ── LLM Router ────────────────────────────────────────────────────────────────

# Valid technique set for LLM decision validation
_VALID_TECHNIQUES: set[str] = {t.value for t in Technique}

# Routing table — used for validation (LLM path) and heuristic fallback (CLI path)
_ROUTING_TABLE: dict[tuple[str, str], Technique] = {
    # (independence, cognitive_load) → technique
    ("continuous",  "low"):    Technique.ZERO_SHOT,
    ("independent", "low"):    Technique.ZERO_SHOT,
    ("continuous",  "medium"): Technique.FEW_SHOT,
    ("independent", "medium"): Technique.ZERO_SHOT_COT,
    ("continuous",  "high"):   Technique.FEW_SHOT_COT,
    ("independent", "high"):   Technique.TREE_OF_THOUGHT,
}

_RATIONALE: dict[Technique, str] = {
    Technique.ZERO_SHOT:      "Low load — direct instruction suffices.",
    Technique.FEW_SHOT:       "Fixed I/O pattern expected — examples anchor output format.",
    Technique.ZERO_SHOT_COT:  "Multi-step reasoning needed, no examples provided.",
    Technique.FEW_SHOT_COT:   "Complex reasoning with provided examples — relay pattern.",
    Technique.STEP_BACK:      "Vague or legacy — abstract to principles first.",
    Technique.LEAST_TO_MOST:  "Decomposable into ordered subproblems.",
    Technique.TREE_OF_THOUGHT: "High risk, multi-path — explore + evaluate + prune.",
}


def route_technique(
    task: str,
    context,
    llm_decision: dict[str, str] | None = None,
) -> Analysis:
    """LLM Router: independence × cognitive load → technique.

    Primary path: LLM decides (via system prompt Router instructions).
      - LLM produces {"technique": "tree-of-thought", "rationale": "...",
                       "independence": "independent", "cognitive_load": "high"}
      - This function validates and returns the Analysis.

    Fallback path (CLI / no LLM decision):
      - Keyword-based heuristic classification → routing table lookup.
      - This path runs when llm_decision is None (e.g. `python loop.py --task "..."`).
    """
    # ── Primary: LLM decision ──
    if llm_decision:
        tech = llm_decision.get("technique", "")
        if tech in _VALID_TECHNIQUES:
            return Analysis(
                technique=tech,
                rationale=llm_decision.get("rationale", _RATIONALE.get(Technique(tech), "")),
                independence=llm_decision.get("independence", "independent"),
                cognitive_load=llm_decision.get("cognitive_load", "medium"),
            )
        # LLM decision invalid → fall through to heuristic

    # ── Fallback: heuristic keyword matching (CLI mode) ──
    return _heuristic_route(task, context)


def _heuristic_route(task: str, context) -> Analysis:
    """Keyword-based fallback when LLM decision is unavailable."""
    independence = _heuristic_independence(task, context)
    load = _heuristic_cognitive_load(task, context)
    key = (independence.value, load.value)
    technique = _ROUTING_TABLE.get(key, Technique.ZERO_SHOT)

    return Analysis(
        technique=technique.value,
        rationale=_RATIONALE.get(technique, "Default route."),
        independence=independence.value,
        cognitive_load=load.value,
    )


def _heuristic_independence(task: str, context) -> Independence:
    """Keyword heuristic: tasks mentioning modifications are continuous."""
    continuous_signals = [
        "fix", "modify", "update", "change", "refactor", "extend",
        "add to", "improve", "debug", "修复", "修改", "改进",
    ]
    task_lower = task.lower()
    if any(s in task_lower for s in continuous_signals):
        return Independence.CONTINUOUS
    if context.session_context and any(
        s in (context.session_context or "").lower() for s in ["continuing", "next step"]
    ):
        return Independence.CONTINUOUS
    return Independence.INDEPENDENT


def _heuristic_cognitive_load(task: str, context) -> CognitiveLoad:
    """Keyword heuristic: security/crypto → high; rename/format → low."""
    high_load_signals = [
        "security", "audit", "crypto", "encrypt", "concurrent",
        "thread", "transaction", "rollback", "安全", "审计", "加密",
        "assembly", "compile", "compiler", "protocol",
    ]
    low_load_signals = [
        "rename", "format", "comment", "config", "readme",
        "simple", "basic", "重命名", "格式化", "注释",
    ]
    task_lower = task.lower()
    if any(s in task_lower for s in high_load_signals):
        return CognitiveLoad.HIGH
    if any(s in task_lower for s in low_load_signals):
        return CognitiveLoad.LOW
    word_count = len(task.split())
    return CognitiveLoad.MEDIUM if word_count > 8 else CognitiveLoad.LOW


# ── 8-Section Prompt Builder ──────────────────────────────────────────────────

_SECTION_TEMPLATES: dict[str, str] = {
    "1": "## 1. 角色 (Role)\n\n{role}\n",
    "2": "## 2. 任务 (Task)\n\n{task}\n",
    "3": "## 3. 输入 (Input)\n\n{input}\n",
    "4": "## 4. 输出格式 (Output Format)\n\n{output_format}\n",
    "5": "## 5. 格式参考示例 (Examples)\n\n{examples}\n",
    "6": "## 6. 具体实现要求 (Implementation Requirements)\n\n{requirements}\n",
    "7": "## 7. 硬约束 (Hard Constraints)\n\n{constraints}\n",
    "8": "## 8. 生成要求 (Generation Requirements)\n\n{generation}\n",
}


def build_8_section(
    task: str,
    technique: Technique,
    analysis: Analysis,
    domain: DomainKnowledge | None,
    hard_constraints: list[str],
    tech_stack: str = "",
) -> str:
    """Assemble the 8-section structured prompt.

    Invariant rules enforced:
      - Section 5 never before Section 3
      - Section 5 never contains meta-examples (examples of prompt design)
      - Sections 1-8 always present in order
    """
    sections: dict[str, str] = {}

    # 1. Role
    role_parts = ["You are an expert software engineer."]
    if tech_stack:
        role_parts.append(f"Tech stack: {tech_stack}.")
    sections["1"] = _SECTION_TEMPLATES["1"].format(role="\n".join(role_parts))

    # 2. Task — one unambiguous sentence
    sections["2"] = _SECTION_TEMPLATES["2"].format(task=task)

    # 3. Input — what the model receives
    input_text = "The codebase or data relevant to this task. See attached context."
    sections["3"] = _SECTION_TEMPLATES["3"].format(input=input_text)

    # 4. Output Format
    output_items = _get_output_format(technique)
    sections["4"] = _SECTION_TEMPLATES["4"].format(
        output_format="\n".join(f"{i+1}. {item}" for i, item in enumerate(output_items))
    )

    # 5. Examples — conditional on domain knowledge
    sections["5"] = _build_section_5(technique, domain)

    # 6. Implementation Requirements
    sections["6"] = _build_section_6(technique, output_items)

    # 7. Hard Constraints — injected from vault GLOBAL + task-specific
    if hard_constraints:
        constraint_lines = "\n".join(f"- {c}" for c in hard_constraints)
    else:
        constraint_lines = "- Follow standard best practices."
    sections["7"] = _SECTION_TEMPLATES["7"].format(constraints=constraint_lines)

    # 8. Generation Requirements
    gen_reqs = _get_generation_requirements(technique)
    sections["8"] = _SECTION_TEMPLATES["8"].format(generation="\n".join(f"- {r}" for r in gen_reqs))

    # Assemble in order
    return "".join(sections[str(i)] for i in range(1, 9))


def _get_output_format(technique: Technique) -> list[str]:
    """Deliverables list per technique."""
    defaults = [
        "Complete, production-ready code",
        "Brief explanation of key design decisions",
    ]
    if technique == Technique.TREE_OF_THOUGHT:
        return [
            "Search strategy declaration (beam/dfs/expert-panel)",
            "Candidate solutions with evaluation scores",
            "Final selected solution with rationale",
            "Complete implementation of the selected solution",
        ]
    if technique == Technique.LEAST_TO_MOST:
        return [
            "Ordered subproblem decomposition (4-6 subproblems)",
            "Solution to each subproblem with dependencies noted",
            "Integrated final implementation",
        ]
    if technique in (Technique.ZERO_SHOT_COT, Technique.FEW_SHOT_COT):
        return [
            "Step-by-step reasoning trace",
            "Final answer or implementation",
        ]
    return defaults


def _build_section_5(technique: Technique, domain: DomainKnowledge | None) -> str:
    """Conditional case generation — section 5 lives between Input (3) and Requirements (6).

    CRITICAL: If no domain knowledge, section 5 is empty — not filled with guesses.
    """
    if domain is None:
        return _SECTION_TEMPLATES["5"].format(examples="[No domain knowledge provided — section intentionally empty.]")

    has_data = any([
        domain.sample_data,
        domain.input_output_pairs,
        domain.reference_implementation,
    ])
    if not has_data:
        return _SECTION_TEMPLATES["5"].format(examples="[No domain knowledge provided — section intentionally empty.]")

    # Domain knowledge exists → generate technique-specific examples
    if technique == Technique.FEW_SHOT and domain.input_output_pairs:
        pairs = domain.input_output_pairs[:3]
        example_lines = []
        for i, pair in enumerate(pairs, 1):
            example_lines.append(f"**Example {i}**")
            example_lines.append(f"Input: {pair.get('input', '')}")
            example_lines.append(f"Output: {pair.get('output', '')}")
            example_lines.append("")
        return _SECTION_TEMPLATES["5"].format(examples="\n".join(example_lines))

    if technique == Technique.FEW_SHOT_COT and domain.input_output_pairs:
        pairs = domain.input_output_pairs[:2]
        example_lines = []
        for i, pair in enumerate(pairs, 1):
            example_lines.append(f"**Example {i}**")
            example_lines.append(f"Input: {pair.get('input', '')}")
            example_lines.append(f"Reasoning: {pair.get('reasoning', '[reasoning steps]')}")
            example_lines.append(f"Output: {pair.get('output', '')}")
            example_lines.append("")
        return _SECTION_TEMPLATES["5"].format(examples="\n".join(example_lines))

    if technique == Technique.TREE_OF_THOUGHT and domain.reference_implementation:
        return _SECTION_TEMPLATES["5"].format(
            examples=f"Reference implementation for context:\n```\n{domain.reference_implementation}\n```"
        )

    # Other techniques: domain knowledge exists but isn't I/O pairs — provide as context
    return _SECTION_TEMPLATES["5"].format(
        examples=f"Domain context (for reference, not meta-examples):\n{_summarise_domain(domain)}"
    )


def _build_section_6(technique: Technique, deliverables: list[str]) -> str:
    """One subsection per deliverable from section 4."""
    lines = []
    for item in deliverables:
        lines.append(f"### {item}")
        lines.append("Implement as described above. Ensure correctness and completeness.")
        lines.append("")
    technique_extras: dict[Technique, str] = {
        Technique.TREE_OF_THOUGHT: "- **Evaluation criteria**: correctness, security, performance, maintainability.",
        Technique.LEAST_TO_MOST: "- **Dependency resolution**: each subproblem must build on the output of the previous.",
        Technique.STEP_BACK: "- **Abstraction first**: derive principles before implementation.",
    }
    if technique in technique_extras:
        lines.append(technique_extras[technique])
    return _SECTION_TEMPLATES["6"].format(requirements="\n".join(lines))


def _get_generation_requirements(technique: Technique) -> list[str]:
    """Acceptance criteria."""
    common = [
        "Code must be syntactically correct and runnable without modification",
        "Follow the project's existing code style and conventions",
    ]
    extras: dict[Technique, list[str]] = {
        Technique.TREE_OF_THOUGHT: [
            "Each candidate solution must be independently evaluated",
            "The final selection must reference evaluation scores",
        ],
        Technique.LEAST_TO_MOST: [
            "Subproblems must be solved in dependency order",
            "The final integration must reference each subproblem's output",
        ],
    }
    return common + extras.get(technique, [])


def _summarise_domain(domain: DomainKnowledge) -> str:
    """Brief domain context summary (not a meta-example)."""
    parts = []
    if domain.field_definitions:
        parts.append(f"Fields: {json.dumps(domain.field_definitions, ensure_ascii=False)}")
    if domain.reference_ranges:
        parts.append(f"Ranges: {json.dumps(domain.reference_ranges, ensure_ascii=False)}")
    if domain.specifications:
        parts.append(f"Specs: {domain.specifications[:200]}")
    return "\n".join(parts)


# ── Shared scoring ─────────────────────────────────────────────────────────────

def score_quality(feedback) -> int:
    """Score execution feedback 1-5. Single source of truth — used by both
    builder and engine to avoid duplicate logic.

    Handles both ExecutionFeedback dataclass and plain dict (from JSON).
    """
    if feedback is None:
        return 0

    # Normalise to dict for uniform access
    if hasattr(feedback, "success"):
        fb = {"success": feedback.success,
              "constraint_violations": getattr(feedback, "constraint_violations", []),
              "manual_fixes_needed": getattr(feedback, "manual_fixes_needed", "")}
    elif isinstance(feedback, dict):
        fb = feedback
    else:
        return 0

    if fb.get("success") and not fb.get("constraint_violations") and not fb.get("manual_fixes_needed"):
        return 5
    if fb.get("success") and not fb.get("constraint_violations"):
        return 4
    if fb.get("success"):
        return 3
    if fb.get("constraint_violations"):
        return 2
    return 1


# ── Main Builder Pipeline ─────────────────────────────────────────────────────

def build(request: PromptCraftRequest, hydrate_results: dict[str, Any] | None = None) -> BuildResult:
    """Execute the complete single-build pipeline.

    Args:
        request: The PromptCraftRequest from the main agent.
        hydrate_results: Pre-loaded vault results (hydrate.py output).
                        If None, the builder operates without vault context.

    Returns:
        BuildResult containing the response and key decisions for the Engine.
    """
    # ── Self-awareness (embedded, not a separate step) ──
    # hydrate_results already contains:
    #   - global_entries: GLOBAL constraints → unconditionally injected
    #   - results: similar past prompts + their feedback
    # The Router and builder consume these naturally — no separate "check" step.
    global_constraints: list[str] = []
    past_feedback: dict[str, Any] = {}
    if hydrate_results:
        for entry in hydrate_results.get("global_entries", []):
            for c in entry.get("hard_constraints_added", []):
                if c not in global_constraints:
                    global_constraints.append(c)
        # Past feedback for self-awareness
        for result in hydrate_results.get("results", []):
            score = result.get("feedback", {}).get("quality_score")
            if score is not None:
                past_feedback[result.get("task_id", "")] = {
                    "score": score,
                    "technique": result.get("technique", ""),
                    "notes": result.get("feedback", {}).get("improvement_notes", ""),
                }

    # ── Task ID ──
    task_id = request.task_id or make_task_id(request.task)

    # ── Route ──
    analysis = route_technique(request.task, request.context)

    # Apply past-feedback learnings (self-awareness in action):
    # If a past similar task used technique X and got low score, reconsider.
    for past_id, fb in past_feedback.items():
        if fb["score"] and fb["score"] <= 2 and fb["technique"] == analysis.technique:
            # Same technique previously failed — flag in rationale
            analysis.rationale += (
                f" (Note: similar task '{past_id}' used {analysis.technique} "
                f"and scored {fb['score']}/5. Consider alternative.)"
            )
            break

    # ── Determine importance ──
    importance = _determine_importance(request.mode, global_constraints)

    # ── Build 8-section prompt ──
    prompt_text = build_8_section(
        task=request.task,
        technique=Technique(analysis.technique),
        analysis=analysis,
        domain=request.context.domain_knowledge,
        hard_constraints=global_constraints,
        tech_stack=request.context.tech_stack or "",
    )

    # ── Assemble response ──
    vault_ref = new_vault_ref(task_id) if request.mode != Mode.QUICK else None

    response = PromptCraftResponse(
        status=AgentStatus.OK,
        prompt=prompt_text,
        analysis=analysis,
        metadata=Metadata(
            task_id=task_id,
            skill_used=analysis.technique,
            hard_constraints=global_constraints,
            key_decisions=[analysis.technique, importance.value],
            summary=Summary(
                goal=request.task[:200],
                technique=analysis.technique,
                importance=importance.value,
                open_questions=[],
            ),
        ),
        vault=vault_ref,
    )

    return BuildResult(
        response=response,
        technique=analysis.technique,
        importance=importance.value,
        hard_constraints=global_constraints,
        key_decisions=[analysis.technique],
    )


def _determine_importance(mode: Mode, global_constraints: list[str]) -> Importance:
    """Default importance: STAGE for full builds, WORKING for quick, REFERENCE for feedback."""
    if mode == Mode.FEEDBACK:
        return Importance.REFERENCE
    if mode == Mode.QUICK:
        return Importance.WORKING
    # Full build: default to STAGE unless constraints look universal
    if global_constraints:
        # If there are GLOBAL constraints being inherited, the result is at least STAGE
        return Importance.STAGE
    return Importance.WORKING


# ── Feedback Builder ──────────────────────────────────────────────────────────

def build_feedback(
    request: PromptCraftRequest,
    hydrate_results: dict[str, Any] | None,
) -> BuildResult:
    """Feedback mode: assess execution results and generate improvement notes.

    This is the "reflect and refine" side of the self-awareness loop.
    """
    if not request.feedback:
        return BuildResult(
            response=PromptCraftResponse(
                status=AgentStatus.ERROR,
                error="Feedback mode requires ExecutionFeedback in request.",
            ),
            technique="",
            importance=Importance.REFERENCE.value,
        )

    quality = score_quality(request.feedback)

    notes_parts = []
    if request.feedback.constraint_violations:
        notes_parts.append(f"Violated constraints: {', '.join(request.feedback.constraint_violations)}.")
    if request.feedback.manual_fixes_needed:
        notes_parts.append(f"Manual fixes required: {request.feedback.manual_fixes_needed}")

    return BuildResult(
        response=PromptCraftResponse(
            status=AgentStatus.OK,
            prompt=None,  # Feedback mode doesn't generate a new prompt
            analysis=None,
            metadata=Metadata(
                task_id=request.task_id or make_task_id(request.task),
                skill_used=hydrate_results.get("technique", "") if hydrate_results else "",
                summary=Summary(
                    goal=f"Feedback for {request.task_id}",
                    technique="feedback",
                    importance=Importance.REFERENCE.value,
                    summary_text=f"Quality: {quality}/5. " + " ".join(notes_parts),
                ),
            ),
            vault=new_vault_ref(request.task_id or "feedback"),
        ),
        technique="feedback",
        importance=Importance.REFERENCE.value,
        key_decisions=[f"quality_score={quality}"],
    )
