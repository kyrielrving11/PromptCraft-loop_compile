"""PromptCraft Agent — Technique router + quality scoring.

The two pure-function responsibilities that stay in Python:
  1. Technique selection — keyword heuristic, fast + zero-cost
  2. Quality scoring — deterministic 1-5 from feedback signals

Prompt generation is the LLM sub-agent's job — it reads the selected
technique reference file from skills/prompt-techniques/references/
and applies the technique to generate the 8-section prompt.
"""

from __future__ import annotations

from typing import Any

from protocol import Analysis, Technique


# ── Technique Router ────────────────────────────────────────────────────────────

# Routing table: (independence, cognitive_load) → technique
_ROUTING_TABLE: dict[tuple[str, str], Technique] = {
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

# Technique name → reference file path
TECHNIQUE_REFERENCE: dict[str, str] = {
    "zero-shot":       "skills/prompt-techniques/references/zero-shot.md",
    "few-shot":        "skills/prompt-techniques/references/few-shot.md",
    "zero-shot-cot":   "skills/prompt-techniques/references/chain-of-thought.md",
    "few-shot-cot":    "skills/prompt-techniques/references/chain-of-thought.md",
    "step-back":       "skills/prompt-techniques/references/step-back.md",
    "least-to-most":   "skills/prompt-techniques/references/least-to-most.md",
    "tree-of-thought": "skills/prompt-techniques/references/tree-of-thought.md",
}

# Keyword sets for heuristic classification
_HIGH_LOAD_WORDS = {
    "security", "audit", "crypto", "encrypt", "concurrent",
    "thread", "transaction", "rollback", "compile", "protocol",
}
_LOW_LOAD_WORDS = {
    "rename", "format", "comment", "config", "readme", "simple", "basic",
}
_CONTINUOUS_WORDS = {
    "fix", "modify", "update", "change", "refactor", "extend",
    "add", "improve", "debug",
}


def route_technique(task: str, context=None) -> Analysis:
    """Select the best prompt-engineering technique via keyword heuristic.

    Determines independence (continuous vs independent) and cognitive load
    (low/medium/high) from task keywords, then looks up the technique in
    the routing table.
    """
    task_lower = task.lower()

    # ── Independence ──
    continuous = any(w in task_lower for w in _CONTINUOUS_WORDS)
    if not continuous and context and getattr(context, 'session_context', None):
        continuous = any(
            w in str(context.session_context).lower()
            for w in ("continuing", "next step")
        )
    independence = "continuous" if continuous else "independent"

    # ── Cognitive load ──
    if any(w in task_lower for w in _HIGH_LOAD_WORDS):
        load = "high"
    elif any(w in task_lower for w in _LOW_LOAD_WORDS):
        load = "low"
    else:
        load = "medium" if len(task.split()) > 8 else "low"

    technique = _ROUTING_TABLE.get((independence, load), Technique.ZERO_SHOT)
    return Analysis(
        technique=technique.value,
        rationale=_RATIONALE.get(technique, "Default route."),
        independence=independence,
        cognitive_load=load,
        reference_file=TECHNIQUE_REFERENCE.get(technique.value, ""),
    )


# ── Quality Scoring ─────────────────────────────────────────────────────────────

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


# ── Vault context helpers ───────────────────────────────────────────────────────

# ── Adaptive Technique Routing (v3.5) ──────────────────────────────────────────

# Fallback chain: when the current technique yields 2+ consecutive low-quality
# rounds within the same loop, rotate to the next technique. This is a
# quality-driven correction on top of the keyword heuristic — not a replacement.
_TECHNIQUE_FALLBACK: dict[str, str] = {
    "zero-shot":       "few-shot",
    "few-shot":        "zero-shot-cot",
    "zero-shot-cot":   "few-shot-cot",
    "few-shot-cot":    "tree-of-thought",
    "step-back":       "least-to-most",
    "least-to-most":   "tree-of-thought",
    "tree-of-thought": "tree-of-thought",  # Ceiling — no further rotation
}

_ADAPTIVE_LOW_QUALITY_THRESHOLD = 3   # Quality < this is "low"
_ADAPTIVE_CONSECUTIVE_ROUNDS = 2       # Consecutive low-quality rounds to trigger rotation


def _count_consecutive_low_quality(
    technique: str,
    loop_id: str,
    vault_context: dict[str, Any] | None,
) -> int:
    """Count how many consecutive recent rounds used `technique` and scored < 3."""
    if vault_context is None:
        return 0
    results = vault_context.get("results", [])
    if not results:
        return 0

    # Filter to this loop, sort by round descending
    rounds: list[dict[str, Any]] = []
    for r in results:
        lineage = r.get("loop_lineage") or r.get("lineage") or {}
        if lineage.get("loop_id") == loop_id:
            # technique_used lives as skill_used in JSON vault, technique_used in markdown
            technique_used = r.get("technique_used") or r.get("skill_used") or ""
            rounds.append({
                "round": lineage.get("round", 0),
                "quality_score": lineage.get("quality_score", 0),
                "technique_used": technique_used,
            })
    rounds.sort(key=lambda x: x["round"], reverse=True)

    count = 0
    for rnd in rounds:
        # Only count rounds that used THIS technique
        if rnd["technique_used"] and rnd["technique_used"] != technique:
            break  # Different technique — break the consecutive chain
        if rnd["quality_score"] > 0 and rnd["quality_score"] < _ADAPTIVE_LOW_QUALITY_THRESHOLD:
            count += 1
        else:
            break  # High quality or zero score (no feedback) — break the chain
    return count


def route_technique_adaptive(
    task: str,
    vault_context: dict[str, Any] | None = None,
    loop_id: str = "",
) -> Analysis:
    """Select the best technique via keyword heuristic, then apply quality-driven
    fallback rotation if the current technique has underperformed.

    Keeps the original keyword analysis intact — rotation is recorded in
    Analysis.was_rotated and the rationale is appended.
    """
    # Step 1: Keyword heuristic (always)
    analysis = route_technique(task)

    if not vault_context or not loop_id:
        return analysis

    # Step 2: Check if current technique needs rotation
    technique = analysis.technique
    low_count = _count_consecutive_low_quality(technique, loop_id, vault_context)

    if low_count < _ADAPTIVE_CONSECUTIVE_ROUNDS:
        return analysis

    # Step 3: Rotate
    fallback = _TECHNIQUE_FALLBACK.get(technique, technique)
    if fallback == technique:
        return analysis  # Already at ceiling

    original_technique = technique
    original_rationale = analysis.rationale

    return Analysis(
        technique=fallback,
        rationale=(
            f"{original_rationale} [ROTATED: {original_technique} → {fallback} — "
            f"{low_count} consecutive low-quality rounds (score < {_ADAPTIVE_LOW_QUALITY_THRESHOLD})]"
        ),
        independence=analysis.independence,
        cognitive_load=(
            "high" if fallback in ("tree-of-thought", "few-shot-cot")
            else "medium" if fallback in ("zero-shot-cot", "least-to-most")
            else analysis.cognitive_load
        ),
        reference_file=TECHNIQUE_REFERENCE.get(fallback, analysis.reference_file),
        was_rotated=True,
    )


def extract_global_constraints(hydrate_results: dict[str, Any] | None) -> list[str]:
    """Extract GLOBAL hard constraints from hydrate results.

    GLOBAL entries are always returned by hydrate.py regardless of query match.
    These constraints must be injected into every generated prompt.
    """
    constraints: list[str] = []
    if not hydrate_results:
        return constraints
    for entry in hydrate_results.get("global_entries", []):
        for c in entry.get("hard_constraints_added", []):
            if c not in constraints:
                constraints.append(c)
    return constraints
