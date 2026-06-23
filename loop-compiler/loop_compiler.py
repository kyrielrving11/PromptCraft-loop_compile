"""PromptCraft-loop_compile — Loop Compiler (v3.5 core).

Pure-function module for per-loop-iteration prompt compilation. Called by
engine.invoke_loop_compile() — never directly by the sub-agent adapter.

Two layers:
  Layer 1 (Hard Gates): decide_level() — 4-gate routing that CAN change compile level.
  Layer 2 (Soft Advisories): compute_advisories() — warnings/alignment/health, NEVER
    change compile level directly.

Compilation: compile_l0() / compile_l1() / compile_l2() produce the actual prompt.
Persistence: lineage dual-write (vault JSON + markdown frontmatter).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from protocol import (
    LoopCompileRequest,
    LoopCompileResponse,
    LoopHealth,
    LoopObjective,
    TaskAlignment,
    RollingSummary,
)
from builder import route_technique, route_technique_adaptive


# ── Repair Cue Detection ────────────────────────────────────────────────────────

_REPAIR_CUES: tuple[str, ...] = (
    "fix", "repair", "revise", "correct", "polish", "bug", "error",
    "修复", "修改", "修正", "纠错", "补充", "改一下",
)


def _detects_repair_signal(request: LoopCompileRequest) -> bool:
    """Check new_since_last_round and last_round_result for repair/fix cues.

    Inherited from PromptCraft-MCP classifier.py REPAIR_CUES detection.
    Zero-cost keyword match — same pattern as builder.route_technique()."""
    text = (request.new_since_last_round or "").lower()
    if request.last_round_result:
        text += " " + (request.last_round_result.output_summary or "").lower()
        if request.last_round_result.manual_fixes_needed:
            text += " " + request.last_round_result.manual_fixes_needed.lower()
    return any(cue in text for cue in _REPAIR_CUES)


# ── Tokenization helpers ───────────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """Minimal CJK-aware tokenizer — splits on whitespace + word boundaries.

    ASCII tokens are lowercased for case-insensitive matching (consistent
    with hydrate.py). CJK characters are preserved as-is.
    """
    tokens = text.split()
    result: set[str] = set()
    for token in tokens:
        token = token.strip(".,;:!?()[]{}'\"")
        if len(token) >= 2:
            # Lowercase ASCII for case-insensitive matching
            result.add(token.lower() if token.isascii() else token)
        # Add individual CJK chars as standalone tokens
        for ch in token:
            if '一' <= ch <= '鿿' or '぀' <= ch <= 'ヿ':
                result.add(ch)
    return result


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two token sets.

    Returns 0.0 when either set is empty — two empty texts share no
    information. Consistent with hydrate.py's _jaccard.
    """
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union > 0 else 0.0


# ── Goal identity ───────────────────────────────────────────────────────────────

def compute_goal_text_hash(task: str) -> str:
    """SHA256 of normalized task string — auxiliary key, not primary gate."""
    normalized = re.sub(r'\s+', ' ', (task or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def derive_goal_id(loop_id: str, task: str, explicit_goal_id: str = "") -> str:
    """Derive goal_id — uses explicit ID if set, otherwise fallback.

    Fallback: loop_id + task[:60] (compatibility only, not recommended).
    Callers should provide stable goal_id for L0/L1 to work."""
    if explicit_goal_id:
        return explicit_goal_id
    task_prefix = re.sub(r'\s+', '-', (task or "unnamed")[:60].strip().lower())
    task_prefix = re.sub(r'[^a-z0-9-]', '', task_prefix)
    return f"{loop_id}:{task_prefix}" if loop_id else task_prefix


# ── Previous round lookup ──────────────────────────────────────────────────────

@dataclass
class _PreviousRound:
    """Lightweight previous-round snapshot extracted from vault context."""
    goal_id: str = ""
    goal_text_hash: str = ""
    quality_score: int = 0
    success: bool = True
    task: str = ""
    constraints_active: list[str] = field(default_factory=list)
    prompt_text: str = ""  # Full compiled prompt from previous round (L0 cache)


def get_previous_round(
    loop_id: str, round_num: int, vault_context: dict[str, Any] | None,
) -> _PreviousRound | None:
    """Extract the previous round's state from vault context.

    Vault context is the parsed JSON output from hydrate.py — results
    keyed by loop_id and round number.
    """
    if vault_context is None:
        return None
    results = vault_context.get("results", [])
    if not results:
        return None
    for r in results:
        lineage = r.get("loop_lineage") or r.get("lineage") or {}
        if lineage.get("loop_id") == loop_id and lineage.get("round") == round_num:
            return _PreviousRound(
                goal_id=lineage.get("goal_id", ""),
                goal_text_hash=lineage.get("goal_text_hash", ""),
                quality_score=lineage.get("quality_score", 0),
                success=r.get("success", True),
                task=r.get("task", "") or r.get("user_intent", ""),
                constraints_active=lineage.get("constraints_active", []),
                prompt_text=r.get("full_prompt", ""),
            )
    return None


def get_recent_rounds(
    loop_id: str, n: int, vault_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return the last N rounds for the given loop_id from vault context."""
    if vault_context is None:
        return []
    results = vault_context.get("results", [])
    rounds: list[dict[str, Any]] = []
    for r in results:
        lineage = r.get("loop_lineage") or r.get("lineage") or {}
        if lineage.get("loop_id") == loop_id:
            rounds.append({
                "quality_score": lineage.get("quality_score", 0),
                "round": lineage.get("round", 0),
                "goal_text_hash": lineage.get("goal_text_hash", ""),
            })
    rounds.sort(key=lambda x: x["round"], reverse=True)
    return rounds[:n]


def get_previous_round_task(
    loop_id: str, round_num: int, vault_context: dict[str, Any] | None,
) -> str:
    """Get the task text from a specific previous round."""
    prev = get_previous_round(loop_id, round_num, vault_context)
    return prev.task if prev else ""


def vault_get_loop_objective(
    loop_id: str, vault_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Find the loop_objective entry for this loop in vault context."""
    if vault_context is None:
        return None
    # Check global entries first (loop objectives stored as GLOBAL)
    global_entries = vault_context.get("global_entries", [])
    for entry in global_entries:
        lo = entry.get("loop_objective")
        if lo and entry.get("loop_id") == loop_id:
            return {"loop_objective": lo, "loop_id": loop_id}
    # Check results
    for r in vault_context.get("results", []):
        lo = r.get("loop_objective")
        if lo and r.get("loop_id") == loop_id:
            return {"loop_objective": lo, "loop_id": loop_id}
    return None


def _count_consecutive_hash_mismatches(
    loop_id: str, vault_context: dict[str, Any] | None,
) -> int:
    """Count consecutive rounds where goal_text_hash changed but goal_id matched."""
    if vault_context is None:
        return 0
    rounds = get_recent_rounds(loop_id, 20, vault_context)
    if len(rounds) < 2:
        return 0
    count = 0
    for i in range(len(rounds) - 1):
        curr_hash = rounds[i].get("goal_text_hash", "")
        prev_hash = rounds[i + 1].get("goal_text_hash", "")
        if curr_hash and prev_hash and curr_hash != prev_hash:
            count += 1
        else:
            break  # Only count consecutive
    return count


# ── Strategy Collapse Detection ─────────────────────────────────────────────────

def strategy_collapse(
    loop_id: str, vault_context: dict[str, Any] | None,
) -> bool:
    """True if last 3 consecutive rounds all have quality < 3."""
    recent = get_recent_rounds(loop_id, 3, vault_context)
    if len(recent) < 3:
        return False
    return all(r.get("quality_score", 0) < 3 for r in recent)


# ── Constraint Retirement (v3.5) ─────────────────────────────────────────────────

_RETIREMENT_WINDOW = 3  # Number of consecutive silent rounds to trigger retirement


def _compute_constraint_retirement(
    active_constraints: list[str],
    loop_id: str,
    current_round: int,
    vault_context: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """Retire constraints that have shown no activity for _RETIREMENT_WINDOW rounds.

    A constraint is "active" in a round if it appears (case-insensitive substring)
    in the round's task, output_summary, or constraint_violations.

    Returns (pruned_active, newly_retired). Retirement is append-only:
    constraints that are already gone stay gone.
    """
    if not active_constraints or vault_context is None:
        return list(active_constraints), []

    results = vault_context.get("results", [])
    if not results:
        return list(active_constraints), []

    # Build a set of rounds to check: current_round-1 down to current_round-_RETIREMENT_WINDOW
    target_rounds = set(
        range(current_round - _RETIREMENT_WINDOW, current_round)
    )

    # Collect all text per target round for activity detection
    round_texts: dict[int, str] = {}
    for r in results:
        lineage = r.get("loop_lineage") or r.get("lineage") or {}
        if lineage.get("loop_id") != loop_id:
            continue
        rnd = lineage.get("round", 0)
        if rnd not in target_rounds:
            continue
        text = (
            (r.get("task", "") or r.get("user_intent", "")) + " " +
            (lineage.get("task", "")) + " " +
            (r.get("output_summary", ""))
        ).lower()
        # Also check violation lists
        for v in r.get("constraint_violations", []) or []:
            text += " " + str(v).lower()
        round_texts[rnd] = text

    # For each constraint, check if any target round has an activity signal
    retired: list[str] = []
    pruned: list[str] = []
    for constraint in active_constraints:
        c_lower = constraint.lower()
        # Also try hyphen→space normalized form for flexible matching
        c_normalized = c_lower.replace("-", " ")
        is_active = False
        for rnd, text in round_texts.items():
            if c_lower in text or c_normalized in text:
                is_active = True
                break

        if is_active or len(round_texts) < _RETIREMENT_WINDOW:
            pruned.append(constraint)
        else:
            retired.append(constraint)

    return pruned, retired


# ── Rolling Summary (v3.5) ──────────────────────────────────────────────────────

_ROLLING_WINDOW = 5  # Number of past rounds to sample for cross-round synthesis


def _build_rolling_summary(
    loop_id: str,
    current_round: int,
    vault_context: dict[str, Any] | None,
) -> RollingSummary | None:
    """Build a deterministic cross-round knowledge distillation from vault history.

    Samples the last _ROLLING_WINDOW rounds (excluding current) and produces:
      - quality_trajectory: raw scores for trend analysis
      - trajectory_direction: "improving" | "declining" | "stable" | "volatile"
      - what_worked: high-score (>=4) round task summaries
      - recurring_issues: violations appearing in 2+ rounds
      - key_lessons: output_summary from high-score rounds

    Returns None if no history is available. Pure function — no LLM call.
    """
    if vault_context is None:
        return None

    results = vault_context.get("results", [])
    if not results:
        return None

    # Collect rounds matching this loop_id, excluding current_round
    rounds: list[dict[str, Any]] = []
    for r in results:
        lineage = r.get("loop_lineage") or r.get("lineage") or {}
        if lineage.get("loop_id") != loop_id:
            continue
        rnd = lineage.get("round", 0)
        if rnd >= current_round:
            continue  # Exclude current and future rounds
        rounds.append({
            "round": rnd,
            "quality_score": lineage.get("quality_score", 0),
            "task": r.get("task", "") or r.get("user_intent", ""),
            "output_summary": r.get("output_summary", ""),
            "constraint_violations": r.get("constraint_violations", []) or [],
            "technique_used": r.get("technique_used", ""),
        })

    if not rounds:
        return None

    # Sort by round descending, take last _ROLLING_WINDOW
    rounds.sort(key=lambda x: x["round"], reverse=True)
    sampled = rounds[:_ROLLING_WINDOW]

    # ── Quality trajectory ──
    trajectory = [r["quality_score"] for r in reversed(sampled)]  # Chronological order

    # ── Trajectory direction ──
    if len(trajectory) >= 2:
        diffs = [trajectory[i] - trajectory[i - 1] for i in range(1, len(trajectory))]
        if all(d >= 0 for d in diffs) and any(d > 0 for d in diffs):
            direction = "improving"
        elif all(d <= 0 for d in diffs) and any(d < 0 for d in diffs):
            direction = "declining"
        elif all(d == 0 for d in diffs):
            direction = "stable"
        else:
            direction = "volatile"
    else:
        direction = "stable"

    # ── What worked (high-score rounds) ──
    what_worked: list[str] = []
    for r in sampled:
        if r["quality_score"] >= 4 and r["output_summary"]:
            what_worked.append(
                f"R{r['round']} (score={r['quality_score']}, "
                f"{r.get('technique_used', 'n/a')}): {r['output_summary'][:150]}"
            )

    # ── Recurring issues (violations appearing in 2+ rounds) ──
    violation_counts: dict[str, int] = {}
    for r in sampled:
        seen_in_this_round: set[str] = set()
        for v in r.get("constraint_violations", []) or []:
            v_norm = str(v).strip().lower()
            if v_norm and v_norm not in seen_in_this_round:
                violation_counts[v_norm] = violation_counts.get(v_norm, 0) + 1
                seen_in_this_round.add(v_norm)

    recurring_issues = [
        f"{v} (appeared in {count} rounds)"
        for v, count in sorted(violation_counts.items(), key=lambda x: -x[1])
        if count >= 2
    ]

    # ── Key lessons (output summaries from high-score rounds) ──
    key_lessons: list[str] = []
    for r in sampled:
        if r["quality_score"] >= 4 and r["output_summary"]:
            key_lessons.append(
                f"[R{r['round']}] {r['output_summary'][:200]}"
            )

    return RollingSummary(
        quality_trajectory=trajectory,
        trajectory_direction=direction,
        what_worked=what_worked,
        recurring_issues=recurring_issues,
        key_lessons=key_lessons,
        rounds_sampled=len(sampled),
        generated_at_round=current_round,
    )


def _format_rolling_summary_for_prompt(rs: RollingSummary | None) -> str:
    """Format a RollingSummary as a human-readable prompt block for L1/L2 injection."""
    if rs is None or rs.rounds_sampled == 0:
        return ""

    lines = [
        "### Cross-Round Summary (Accumulated)",
        "",
        f"**Sampled**: {rs.rounds_sampled} prior rounds | **Direction**: {rs.trajectory_direction}",
        f"**Quality Trajectory**: {rs.quality_trajectory}",
        "",
    ]

    if rs.what_worked:
        lines.append("**What Worked (score >= 4)**:")
        for w in rs.what_worked:
            lines.append(f"- {w}")
        lines.append("")

    if rs.recurring_issues:
        lines.append("**Recurring Issues (appeared 2+ times)**:")
        for ri in rs.recurring_issues:
            lines.append(f"- ⚠️ {ri}")
        lines.append("")

    if rs.key_lessons:
        lines.append("**Key Lessons From High-Score Rounds**:")
        for kl in rs.key_lessons:
            lines.append(f"- {kl}")
        lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — Hard Gates (can change compile level)
# ═══════════════════════════════════════════════════════════════════════════════

def decide_level(request: LoopCompileRequest, vault_context: dict[str, Any] | None) -> str:
    """Pure function. Returns 'l0', 'l1', or 'l2'.

    Hard gates only — four conditions that can change the compile level.
    Soft advisories (task_alignment, health, repair cue, forward hint)
    are computed separately and returned as warnings — they do NOT gate here.
    """

    # Gate 1: Explicit override (never overrides round 1 or plan_source —
    # those are hard L2 triggers that anchor the loop with a loop_objective)
    if request.force_level != "auto" and request.force_level in ("l0", "l1", "l2"):
        if request.round != 1 and not request.plan_source:
            return request.force_level

    # Gate 2: First call or explicit plan input → full rebuild
    if request.round == 1 or request.plan_source:
        return "l2"

    # Derive goal_id
    goal_id = derive_goal_id(request.loop_id, request.task, request.goal_id)

    prev = get_previous_round(request.loop_id, request.round - 1, vault_context)
    if prev is None:
        return "l2"

    # Gate 3: goal_id stability — the primary hard gate
    if goal_id != prev.goal_id:
        return "l2"

    # Gate 4: Explicit failures or new constraints → patch
    has_new_constraints = bool(request.constraints_from_plan)
    has_new_failures = (
        request.last_round_result is not None
        and not request.last_round_result.success
    )
    # Also check for repair cues — they force L1
    has_repair = _detects_repair_signal(request)

    if has_new_constraints or has_new_failures or has_repair:
        return "l1"

    # Nothing triggered → fast path
    return "l0"


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — Soft Advisories (NEVER change compile level)
# ═══════════════════════════════════════════════════════════════════════════════

def align_task(
    proposed_task: str,
    request: LoopCompileRequest,
    vault_context: dict[str, Any] | None,
) -> TaskAlignment:
    """Validate Agent's proposed next task against Loop Objective. ADVISORY ONLY.

    Returns TaskAlignment with escalation level:
      - none:  aligned (score >= 0.5)
      - warn:  mild drift (0.3 <= score < 0.5)
      - block: severe drift (score < 0.3)

    The 'block' escalation is a STRONG SUGGESTION, not a hard gate.
    """
    objective_entry = vault_get_loop_objective(request.loop_id, vault_context)

    # If a loop_objective was passed inline, use it directly
    obj_data = None
    if request.loop_objective:
        obj_data = request.loop_objective
    elif objective_entry:
        lo = objective_entry.get("loop_objective")
        if isinstance(lo, dict):
            obj_data = lo

    if obj_data is None:
        return TaskAlignment()

    objective = (obj_data.get("objective") if isinstance(obj_data, dict)
                 else getattr(obj_data, "objective", ""))
    success_criteria = (obj_data.get("success_criteria", []) if isinstance(obj_data, dict)
                        else getattr(obj_data, "success_criteria", []))
    hard_constraints = (obj_data.get("hard_constraints", []) if isinstance(obj_data, dict)
                        else getattr(obj_data, "hard_constraints", []))

    proposed_tokens = _tokenize(proposed_task.lower())
    obj_text = f"{objective} {' '.join(success_criteria)} {' '.join(hard_constraints)}".lower()
    obj_tokens = _tokenize(obj_text)
    score = _jaccard(proposed_tokens, obj_tokens) if proposed_tokens and obj_tokens else 1.0

    if score >= 0.5:
        return TaskAlignment(is_aligned=True, alignment_score=round(score, 2))
    elif score >= 0.3:
        return TaskAlignment(
            is_aligned=True,
            alignment_score=round(score, 2),
            warning=(
                f"Proposed task '{proposed_task[:80]}' may be drifting from "
                f"loop objective '{objective}'. Consider narrowing scope."
            ),
            escalation="warn",
        )
    else:
        return TaskAlignment(
            is_aligned=False,
            alignment_score=round(score, 2),
            warning=(
                f"Proposed task '{proposed_task[:80]}' is OFF-OBJECTIVE. "
                f"Loop objective: '{objective}'. Full realignment recommended."
            ),
            escalation="block",
        )


def check_loop_health(
    loop_id: str,
    request: LoopCompileRequest,
    vault_context: dict[str, Any] | None,
) -> LoopHealth:
    """Compute loop health: goal_alignment, constraint_integrity, drift, stability.

    Pure advisory — does NOT change state. The caller decides.
    """
    # Load the loop objective
    objective_entry = vault_get_loop_objective(loop_id, vault_context)
    obj = None
    if request.loop_objective:
        obj = request.loop_objective
    elif objective_entry:
        lo = objective_entry.get("loop_objective")
        if isinstance(lo, dict):
            obj = lo

    if obj is None:
        return LoopHealth()

    objective = obj.get("objective") if isinstance(obj, dict) else getattr(obj, "objective", "")
    success_criteria = (obj.get("success_criteria", []) if isinstance(obj, dict)
                        else getattr(obj, "success_criteria", []))
    hard_constraints = (obj.get("hard_constraints", []) if isinstance(obj, dict)
                        else getattr(obj, "hard_constraints", []))

    # 1. goal_alignment
    if request.task:
        task_tokens = _tokenize(request.task.lower())
        obj_text = f"{objective} {' '.join(success_criteria)} {' '.join(hard_constraints)}".lower()
        obj_tokens = _tokenize(obj_text)
        goal_alignment = _jaccard(task_tokens, obj_tokens) if task_tokens and obj_tokens else 1.0
    else:
        goal_alignment = 1.0

    # 2. constraint_integrity
    constraint_integrity = 1.0
    if request.last_round_result and hard_constraints:
        output_text = request.last_round_result.output_summary.lower()
        present = sum(
            1 for c in hard_constraints
            if any(word in output_text for word in c.lower().split())
        )
        constraint_integrity = present / len(hard_constraints) if hard_constraints else 1.0

    # 3. drift_detected
    drift_detected = _count_consecutive_hash_mismatches(loop_id, vault_context) >= 3

    # 4. strategy_stability: last 3 rounds all quality >= 4
    recent = get_recent_rounds(loop_id, 3, vault_context)
    strategy_stability = all(r.get("quality_score", 0) >= 4 for r in recent) if recent else True

    # 5. task_continuity
    task_continuity = 1.0
    prev_task = get_previous_round_task(loop_id, request.round - 1, vault_context)
    if prev_task and request.task:
        curr_tokens = _tokenize(request.task.lower())
        prev_tokens = _tokenize(prev_task.lower())
        task_continuity = _jaccard(curr_tokens, prev_tokens) if curr_tokens and prev_tokens else 1.0

    # Escalation recommendation
    escalation = "none"
    if goal_alignment < 0.5:
        escalation = "l2"
    elif constraint_integrity < 0.7:
        escalation = "l1"
    elif drift_detected:
        escalation = "l2"

    return LoopHealth(
        goal_alignment=round(goal_alignment, 2),
        constraint_integrity=round(constraint_integrity, 2),
        drift_detected=drift_detected,
        strategy_stability=strategy_stability,
        task_continuity=round(task_continuity, 2),
        escalation_recommended=escalation,
    )


def compute_suggested_next_task(
    loop_id: str, vault_context: dict[str, Any] | None,
) -> str:
    """Forward hint from vault history — what similar loops did next.

    Returns empty string if no relevant history found."""
    if vault_context is None:
        return ""
    results = vault_context.get("results", [])
    for r in results:
        lineage = r.get("loop_lineage") or r.get("lineage") or {}
        if lineage.get("loop_id") == loop_id:
            # Return the most recent round's task as a hint
            task = r.get("task", "") or r.get("user_intent", "")
            if task:
                return f"Previous round focused on: {task[:120]}"
    return ""


def compute_advisories(
    request: LoopCompileRequest,
    vault_context: dict[str, Any] | None,
) -> tuple[list[str], str, TaskAlignment | None, LoopHealth | None]:
    """Compute all soft advisories. Returns (warnings, suggested_next_task, alignment, health).

    These are PURELY ADVISORY. They never change the compile level directly.
    """
    warnings: list[str] = []
    alignment: TaskAlignment | None = None
    health: LoopHealth | None = None
    suggested: str = ""

    # goal_text_hash drift detection
    current_hash = compute_goal_text_hash(request.task)
    prev = get_previous_round(request.loop_id, request.round - 1, vault_context)
    if prev and current_hash != prev.goal_text_hash and prev.goal_text_hash:
        warnings.append(
            f"goal_text_hash changed ({prev.goal_text_hash} → {current_hash}) "
            "but goal_id matched — wording drift detected"
        )

    # Strategy collapse check
    if strategy_collapse(request.loop_id, vault_context):
        warnings.append(
            "strategy_collapse: 3 consecutive low-quality rounds — "
            "consider force_level=L2 rebuild"
        )

    # Repair cue detection
    if _detects_repair_signal(request):
        warnings.append("repair signal detected — L1 patch applied")

    # Task alignment
    if request.next_task_proposal:
        alignment = align_task(request.next_task_proposal, request, vault_context)
        if alignment.escalation != "none":
            warnings.append(f"task_alignment: {alignment.warning}")

    # Loop health
    if request.round % max(request.health_check_interval, 1) == 0:
        health = check_loop_health(request.loop_id, request, vault_context)
        if health.escalation_recommended != "none":
            warnings.append(
                f"loop_health recommends {health.escalation_recommended}: "
                f"goal_alignment={health.goal_alignment:.2f}, "
                f"constraint_integrity={health.constraint_integrity:.2f}, "
                f"task_continuity={health.task_continuity:.2f}"
            )
        if health.drift_detected:
            warnings.append("drift_detected: goal_text_hash diverged 3+ consecutive rounds")

    # Forward hint
    suggested = compute_suggested_next_task(request.loop_id, vault_context)

    return warnings, suggested, alignment, health


# ═══════════════════════════════════════════════════════════════════════════════
# Compilation — L0 / L1 / L2
# ═══════════════════════════════════════════════════════════════════════════════

def extract_objective_from_plan(plan_path: str) -> dict[str, Any] | None:
    """Extract Goal, Success Criteria, and Hard Constraints from a plan/spec file.

    Reads a Markdown file and uses section-heading heuristics to extract
    three structured components. Returns None if the file can't be read
    or no content is extracted.
    """
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    sections: dict[str, list[str]] = {"goal": [], "success": [], "constraints": []}
    current_section: str | None = None

    # Section-heading patterns (English + Chinese)
    goal_patterns = (
        "goal", "objective", "目标", "目的", "意图",
    )
    success_patterns = (
        "success criteria", "acceptance criteria", "验收标准", "成功标准",
        "done when", "完成标准", "交付标准",
    )
    constraint_patterns = (
        "hard constraint", "constraint", "non-goal", "out of scope",
        "硬约束", "约束", "非目标", "不做什么", "限制",
    )

    for line in text.split("\n"):
        stripped = line.strip()
        low = stripped.lower().lstrip("#").lstrip()

        # Detect section heading
        if stripped.startswith("#"):
            if any(p in low for p in goal_patterns):
                current_section = "goal"
                continue
            if any(p in low for p in success_patterns):
                current_section = "success"
                continue
            if any(p in low for p in constraint_patterns):
                current_section = "constraints"
                continue
            # Unrecognized heading → stop collecting
            current_section = None
            continue

        if not current_section:
            continue

        # Collect bullet/list items
        if stripped.startswith(("-", "*", "•")):
            item = stripped.lstrip("-*• ").strip()
            if item and len(item) > 3:
                sections[current_section].append(item)
        elif stripped and current_section == "goal":
            # First non-empty paragraph under Goal heading is the objective
            if not sections["goal"] and len(stripped) > 10:
                sections["goal"].append(stripped)

    if not any(sections.values()):
        return None

    return {
        "objective": sections["goal"][0] if sections["goal"] else "",
        "success_criteria": sections["success"],
        "hard_constraints": sections["constraints"],
    }


def compute_loop_objective_from_task(
    request: LoopCompileRequest,
    vault_context: dict[str, Any] | None,
) -> LoopObjective:
    """Auto-generate a lightweight Loop Objective at round 1.

    When no plan_source or explicit loop_objective is provided, extracts
    the what and why from the task description. The generated objective is:
      - Stored in vault as a GLOBAL entry tagged with loop_id
      - Returned in LoopCompileResponse.loop_objective
      - Checked by check_loop_health() every subsequent round.

    This is a deterministic extraction — no LLM call. For richer objectives,
    use a planning skill and pass plan_source.
    """
    task = request.task or ""
    constraints = list(request.constraints_from_plan)

    # Simple heuristic extraction from task text
    objective = task.strip()[:200]  # First 200 chars as objective

    # Extract implicit success criteria from constraint-like patterns
    success_criteria: list[str] = []
    if "test" in task.lower() or "测试" in task:
        success_criteria.append("All tests pass")
    if "compat" in task.lower() or "兼容" in task:
        success_criteria.append("Backward compatibility maintained")
    if "security" in task.lower() or "安全" in task or "audit" in task.lower():
        success_criteria.append("No security vulnerabilities found")

    # Hard constraints from plan + common defaults
    hard_constraints = list(constraints)

    # Try to extract richer objective from plan_source file
    plan_extracted = None
    if request.plan_source:
        plan_extracted = extract_objective_from_plan(request.plan_source)
        if plan_extracted:
            if plan_extracted.get("objective"):
                objective = plan_extracted["objective"]
            if plan_extracted.get("success_criteria"):
                success_criteria = plan_extracted["success_criteria"]
            if plan_extracted.get("hard_constraints"):
                hard_constraints.extend(plan_extracted["hard_constraints"])
        else:
            hard_constraints.append(f"Follow plan: {request.plan_source}")

    # Fallback: if nothing extracted, add generic criteria
    if not success_criteria:
        success_criteria.append("Task completed successfully")
    if not hard_constraints:
        hard_constraints.append("Do not modify files outside scope")

    return LoopObjective(
        objective=objective,
        success_criteria=success_criteria,
        hard_constraints=hard_constraints,
        created_at_round=1,
        loop_id=request.loop_id,
    )


def compile_l0(
    request: LoopCompileRequest,
    vault_context: dict[str, Any] | None,
    prev_round: _PreviousRound | None = None,
) -> LoopCompileResponse:
    """L0 Fast Path — reuse cached prompt from previous round.

    Retrieves the previous round's full compiled prompt from vault/markdown
    and returns it with the round number bumped. Auto-escalates to L2 if
    no cached prompt is available (e.g., vault corruption, fresh start).
    """
    prev = prev_round or get_previous_round(request.loop_id, request.round - 1, vault_context)

    cached_prompt = prev.prompt_text if prev else ""

    if cached_prompt:
        # Reuse the previous round's prompt, updating only the round header
        prompt = cached_prompt
        diff = f"L0 cache hit — reusing prompt from round {request.round - 1}"
        technique = "cached"
    else:
        # Auto-escalate: no cached prompt available → delegate to L2 full compile.
        # This handles vault corruption, missing markdown files, and edge cases
        # where force_level=L0 was used but no prior state exists.
        l2_response = compile_l2(request, vault_context)
        # Preserve the recompile_level as L0 so the caller knows this was
        # originally an L0 request (the escalate is transparent to health checks)
        l2_response.recompile_level = "l0"
        l2_response.diff_from_previous = (
            "L0 auto-escalated to L2 — no cached prompt available from "
            f"round {request.round - 1}"
        )
        return l2_response

    return LoopCompileResponse(
        status="ok",
        prompt=prompt,
        recompile_level="l0",
        diff_from_previous=diff,
        lineage=[f"{request.loop_id}:r{request.round}"],
        constraints_active=prev.constraints_active if prev else [],
        constraints_retired=[],
        technique_used=technique,
        loop_id=request.loop_id,
        round=request.round,
        goal_id=derive_goal_id(request.loop_id, request.task, request.goal_id),
        goal_text_hash=compute_goal_text_hash(request.task),
        plan_source=request.plan_source,
        warnings=[],
    )


def compile_l1(
    request: LoopCompileRequest,
    vault_context: dict[str, Any] | None,
    prev_round: _PreviousRound | None = None,
) -> LoopCompileResponse:
    """L1 Patch Path — inject new constraints/failures into previous prompt.

    goal_id unchanged but new constraints, new failures, or repair signals
    detected. Patches the previous prompt with deltas only.

    v3.5: Constraint retirement prunes constraints with no activity signal
    for _RETIREMENT_WINDOW rounds. Rolling summary injected for cross-round context.
    """
    prev = prev_round or get_previous_round(request.loop_id, request.round - 1, vault_context)
    goal_id = derive_goal_id(request.loop_id, request.task, request.goal_id)

    new_constraints = list(request.constraints_from_plan)
    active_raw = (prev.constraints_active if prev else []) + new_constraints
    # Deduplicate
    active_raw = list(dict.fromkeys(active_raw))

    # ── v3.5: Constraint retirement ──
    active, retired = _compute_constraint_retirement(
        active_raw, request.loop_id, request.round, vault_context,
    )

    # ── v3.5: Rolling summary ──
    rolling_summary = _build_rolling_summary(
        request.loop_id, request.round, vault_context,
    )
    rolling_text = _format_rolling_summary_for_prompt(rolling_summary)

    violations: list[str] = []
    if request.last_round_result and request.last_round_result.constraint_violations:
        violations = request.last_round_result.constraint_violations

    diff_parts: list[str] = []
    if new_constraints:
        diff_parts.append(f"new constraints: {new_constraints}")
    if retired:
        diff_parts.append(f"retired constraints: {retired}")
    if violations:
        diff_parts.append(f"violations from last round: {violations}")
    if request.new_since_last_round:
        diff_parts.append(f"delta: {request.new_since_last_round[:200]}")

    # Build patched prompt
    lines = [
        f"## Loop Round {request.round} — L1 Patch",
        "",
        f"**Goal**: {request.task}",
        f"**Loop ID**: {request.loop_id}",
        f"**Goal ID**: {goal_id}",
        "",
    ]

    # ── v3.5: Rolling summary (injected early — constraints reference it) ──
    if rolling_text:
        lines.append(rolling_text)
        lines.append("")

    if active:
        lines.append("### Active Constraints (inherited + new, pruned)")
        for c in active:
            lines.append(f"- {c}")
        lines.append("")

    if retired:
        lines.append("### Retired Constraints (no recent activity)")
        for c in retired:
            lines.append(f"- ~{c}~")
        lines.append("")

    if violations:
        lines.append("### Violations From Last Round (must fix)")
        for v in violations:
            lines.append(f"- {v}")
        lines.append("")

    if request.new_since_last_round:
        lines.append(f"### What Changed Since Last Round")
        lines.append(request.new_since_last_round)
        lines.append("")

    if request.last_round_result and request.last_round_result.output_summary:
        lines.append(f"### Last Round Summary")
        lines.append(request.last_round_result.output_summary)
        lines.append("")

    lines.append("### Task")
    lines.append(request.task)

    return LoopCompileResponse(
        status="ok",
        prompt="\n".join(lines),
        recompile_level="l1",
        diff_from_previous="; ".join(diff_parts) if diff_parts else "Patch applied.",
        lineage=[f"{request.loop_id}:r{request.round}"],
        constraints_active=active,
        constraints_retired=retired,
        technique_used="patch",
        rolling_summary=rolling_summary,
        loop_id=request.loop_id,
        round=request.round,
        goal_id=goal_id,
        goal_text_hash=compute_goal_text_hash(request.task),
        plan_source=request.plan_source,
        warnings=[],
    )


def compile_l2(
    request: LoopCompileRequest,
    vault_context: dict[str, Any] | None,
) -> LoopCompileResponse:
    """L2 Full Recompile — hydrate + route + meta-instruction for LLM generation.

    Used for round 1, goal_id changes, strategy collapse, or explicit force_level=l2.
    Produces a meta-instruction block that tells the LLM which technique reference
    to read and what context to inject — the LLM generates the actual prompt.

    v3.5: Uses adaptive technique routing (quality-driven fallback from
    keyword default). Injects rolling summary for cross-round knowledge
    distillation.
    """
    goal_id = derive_goal_id(request.loop_id, request.task, request.goal_id)

    # ── v3.5: Adaptive technique routing (quality-driven fallback) ──
    analysis = route_technique_adaptive(
        request.task, vault_context, request.loop_id,
    )
    technique = analysis.technique
    reference_file = analysis.reference_file

    # ── v3.5: Rolling summary for cross-round context ──
    rolling_summary = _build_rolling_summary(
        request.loop_id, request.round, vault_context,
    )
    rolling_text = _format_rolling_summary_for_prompt(rolling_summary)

    # Generate loop objective at round 1 if not provided
    loop_objective: LoopObjective | None = None
    if request.round == 1:
        if request.loop_objective:
            loop_objective = request.loop_objective
        else:
            loop_objective = compute_loop_objective_from_task(request, vault_context)

    constraints = list(request.constraints_from_plan)
    if loop_objective:
        constraints = list(dict.fromkeys(constraints + loop_objective.hard_constraints))

    # ── Build meta-instruction (LLM generates the actual prompt) ──
    lines: list[str] = []

    # Header — technique routing
    lines.append(f"## PromptCraft L2 Compile — Round {request.round}")
    lines.append("")
    lines.append("Read the technique reference BEFORE generating the prompt:")
    lines.append(f"  Technique:  {technique}")
    lines.append(f"  Reference:  {reference_file}")
    lines.append(f"  Rationale:  {analysis.rationale}")
    lines.append("")

    # ── v3.5: Rolling summary (injected early for context) ──
    if rolling_text:
        lines.append(rolling_text)
        lines.append("")

    # Loop Objective
    if loop_objective:
        lines.append("### Loop Objective (Anchor)")
        lines.append(f"**Objective**: {loop_objective.objective}")
        if loop_objective.success_criteria:
            lines.append("**Success Criteria**:")
            for sc in loop_objective.success_criteria:
                lines.append(f"- {sc}")
        if loop_objective.hard_constraints:
            lines.append("**Hard Constraints**:")
            for hc in loop_objective.hard_constraints:
                lines.append(f"- {hc}")
        lines.append("")

    # Constraints
    if constraints:
        lines.append("### Active Constraints")
        for c in constraints:
            lines.append(f"- {c}")
        lines.append("")

    # Plan source
    if request.plan_source:
        lines.append(f"**Plan Source**: {request.plan_source}")
        lines.append("")

    # Task
    lines.append("### Task")
    lines.append(request.task)
    lines.append("")

    # Domain
    if request.domain:
        lines.append(f"**Domain**: {request.domain}")
        lines.append("")

    # Loop identity
    lines.append("### Loop Identity")
    lines.append(f"- Loop ID: `{request.loop_id}`")
    lines.append(f"- Goal ID: `{goal_id}`")
    lines.append(f"- Round: {request.round}")
    lines.append("")

    # Generation instructions
    lines.append("### Generation Instructions")
    lines.append(f"1. Read `{reference_file}` — study its structure rules, section count, and format requirements")
    lines.append("2. Generate a complete prompt following that technique's structure")
    lines.append("3. Inject all hard constraints and the loop objective into the prompt")
    lines.append("4. If Cross-Round Summary is present above, incorporate its recurring issues and key lessons")
    lines.append("5. The prompt must be self-contained — ready for a coding agent to execute")
    lines.append("6. Output only the generated prompt — no preamble, no meta-commentary")

    return LoopCompileResponse(
        status="ok",
        prompt="\n".join(lines),
        recompile_level="l2",
        diff_from_previous=(
            "Full recompile — new goal or first call."
            if request.round == 1 or request.plan_source
            else "Full recompile — goal_id changed or strategy collapse."
        ),
        lineage=[f"{request.loop_id}:r{request.round}"],
        constraints_active=constraints,
        constraints_retired=[],
        technique_used=technique,
        reference_file=reference_file,
        rolling_summary=rolling_summary,
        loop_id=request.loop_id,
        round=request.round,
        goal_id=goal_id,
        goal_text_hash=compute_goal_text_hash(request.task),
        loop_objective=loop_objective,
        plan_source=request.plan_source,
        warnings=[],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Top-level compile — ties layers together
# ═══════════════════════════════════════════════════════════════════════════════

def compile_loop(
    request: LoopCompileRequest,
    vault_context: dict[str, Any] | None = None,
) -> LoopCompileResponse:
    """Main entry point — decide level, compile, compute advisories, return response.

    Pure function. No side effects. The caller (engine) handles vault persistence.
    """
    # Layer 1: Decide compile level
    level = decide_level(request, vault_context)

    # Compile at the decided level
    if level == "l0":
        response = compile_l0(request, vault_context)
    elif level == "l1":
        response = compile_l1(request, vault_context)
    else:
        response = compile_l2(request, vault_context)

    # Layer 2: Compute advisories
    warnings, suggested, alignment, health = compute_advisories(request, vault_context)

    # Merge advisories into response
    response.warnings = warnings
    response.suggested_next_task = suggested
    response.task_alignment = alignment
    response.loop_health = health
    response.recompile_level = level

    return response
