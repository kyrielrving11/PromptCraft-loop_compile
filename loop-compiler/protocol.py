"""PromptCraft-loop_compile — I/O protocol definitions.

All data exchanged between the Main Agent and PromptCraft Agent flows through
these structured schemas. This is the contract layer — no implementation logic
lives here, only validation and serialisation.

v3.5: 19 types — loop_compile + build + feedback + review + rolling_summary.
All legacy Tool orchestration types (OverlayConfig, PatternReport, SkillAdvice, etc.) removed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ── Enums ────────────────────────────────────────────────────────────────────

class Mode(str, Enum):
    """Agent invocation mode (v3.4: 3 external + 1 internal)."""
    LOOP_COMPILE = "loop_compile"  # Primary: per-loop prompt compiler
    FEEDBACK     = "feedback"      # Execution result → vault write
    REVIEW       = "review"        # Prompt quality audit
    BUILD        = "build"         # Internal: prompt generation (loop_compile L2 delegation)


class AgentStatus(str, Enum):
    """Top-level response status."""
    OK      = "ok"
    ERROR   = "error"
    STALLED = "stalled"  # Circuit breaker triggered — needs main agent intervention


class Technique(str, Enum):
    """The 7 prompt-engineering techniques."""
    ZERO_SHOT      = "zero-shot"
    FEW_SHOT       = "few-shot"
    ZERO_SHOT_COT  = "zero-shot-cot"
    FEW_SHOT_COT   = "few-shot-cot"
    STEP_BACK      = "step-back"
    LEAST_TO_MOST  = "least-to-most"
    TREE_OF_THOUGHT = "tree-of-thought"


# ── Analysis — Router output ──────────────────────────────────────────────────

@dataclass
class Analysis:
    """Result of technique router evaluation."""
    technique: str
    rationale: str           # One sentence
    independence: str
    cognitive_load: str
    reference_file: str = ""  # Path to technique reference .md
    was_rotated: bool = False # True if adaptive routing changed the technique from keyword default


# ── Vault config ──────────────────────────────────────────────────────────────

@dataclass
class VaultConfig:
    """Vault paths configuration."""
    project_vault: str = ".promptcraft/prompt_vault.json"
    global_vault: str  = "~/.promptcraft/global_vault.json"
    skills_dir: str     = "skills"
    no_global: bool     = False


# ── Request schemas ───────────────────────────────────────────────────────────

@dataclass
class ExecutionFeedback:
    """Execution results passed back for feedback mode."""
    output: str                                 # Actual output produced
    success: bool
    constraint_violations: list[str] = field(default_factory=list)
    manual_fixes_needed: str = ""


@dataclass
class PromptCraftRequest:
    """What the main agent sends to PromptCraft Agent.

    This is the single entry point — every Agent invocation receives one of these.
    """
    task: str                                    # Required: user's core coding task
    mode: Mode = Mode.BUILD
    vault_config: VaultConfig = field(default_factory=VaultConfig)
    # For feedback mode
    feedback: ExecutionFeedback | None = None
    # For overlay mode (Skill enhancement)
    skill_name: str | None = None                # Skill to personalise
    # For iteration tracking (Engine sets these)
    task_id: str | None = None                   # None = first call; set = iteration


# ── Loop Compile types (v3.3) ─────────────────────────────────────────────────

@dataclass
class LoopObjective:
    """Cross-round anchor — created at round 1, checked every round thereafter.

    Stored in vault with importance=GLOBAL for the life of the loop.
    Every loop_lineage entry references it via loop_objective_id."""
    objective: str = ""                  # One-sentence total goal
    success_criteria: list[str] = field(default_factory=list)
    hard_constraints: list[str] = field(default_factory=list)
    created_at_round: int = 1
    loop_id: str = ""


@dataclass
class LoopHealth:
    """Computed every N rounds — informs L0→L1→L2 escalation.

    Pure advisory. The caller (via force_level) always has the final say."""
    goal_alignment: float = 1.0          # 0-1: Jaccard(current task, loop_objective)
    constraint_integrity: float = 1.0    # 0-1: fraction of active constraints in last output
    drift_detected: bool = False         # goal_id matched but goal_text_hash diverged 3+ rounds
    strategy_stability: bool = True      # 3 consecutive rounds with quality >= 4
    task_continuity: float = 1.0         # Jaccard(this_round_task, last_round_task)
    escalation_recommended: str = "none" # "none" | "l1" | "l2"


@dataclass
class RollingSummary:
    """Cross-round knowledge distillation — deterministic synthesis from vault history.

    Built every N rounds (default: health_check_interval). Injected into L1 and L2
    prompts to give the LLM accumulated context beyond single-round output_summary.

    All fields are pure data — no LLM generation, no side effects."""
    quality_trajectory: list[int] = field(default_factory=list)     # Last 5 quality scores
    trajectory_direction: str = ""                                   # "improving" | "declining" | "stable" | "volatile"
    what_worked: list[str] = field(default_factory=list)            # High-score round summaries (>=4)
    recurring_issues: list[str] = field(default_factory=list)       # Violations appearing 2+ times
    key_lessons: list[str] = field(default_factory=list)            # Output summaries from high-score rounds
    rounds_sampled: int = 0                                          # Number of rounds used to build this
    generated_at_round: int = 0                                      # Which round produced this summary


@dataclass
class TaskAlignment:
    """Result of checking an Agent-proposed next task against the Loop Objective.

    PromptCraft does NOT generate the next task — the Agent does. This only
    validates alignment. ALL escalations are advisory — the caller decides."""
    is_aligned: bool = True
    alignment_score: float = 1.0         # 0-1: Jaccard(proposed_task, loop_objective)
    warning: str = ""
    escalation: str = "none"             # "none" | "warn" | "block" (advisory only)


@dataclass
class LoopRoundResult:
    """Execution result from one loop iteration — subset of ExecutionFeedback."""
    round: int = 0
    success: bool = False
    output_summary: str = ""             # 1-2 sentence summary of what happened
    constraint_violations: list[str] = field(default_factory=list)
    manual_fixes_needed: str = ""
    quality_score: int = 0               # 1-5


@dataclass
class LoopCompileRequest:
    """Input to loop_compile mode — called once per loop iteration."""
    mode: str = "loop_compile"
    loop_id: str = ""                    # Unique identifier for this loop session
    round: int = 1                       # Current iteration number (1-indexed)
    goal_id: str = ""                    # STABLE semantic goal key (kebab-case)
    task: str = ""                       # Current task description
    domain: str = ""                     # Optional: "solidity", "python", "devops"

    # ── Agent-proposed next task (anti-drift mechanism) ──
    next_task_proposal: str = ""         # Agent's proposed task for NEXT round

    # ── Loop Objective (cross-round anchor) ──
    loop_objective: LoopObjective | None = None

    # ── Plan-first input ──
    plan_source: str | None = None       # Path to spec/plan/manifest from planning skill
    constraints_from_plan: list[str] = field(default_factory=list)

    # ── Loop state ──
    new_since_last_round: str = ""       # What changed since last round
    last_round_result: LoopRoundResult | None = None

    # ── Overrides ──
    force_level: str = "auto"            # "auto" | "l0" | "l1" | "l2"
    health_check_interval: int = 1       # Run loop health check every N rounds

    # ── Config ──
    vault_config: VaultConfig = field(default_factory=VaultConfig)


@dataclass
class LoopCompileResponse:
    """Output of loop_compile — the compiled prompt for this round."""
    status: str = "ok"                   # "ok" | "error"
    prompt: str = ""                     # The compiled prompt text
    recompile_level: str = "l2"          # "l0" | "l1" | "l2"
    diff_from_previous: str = ""         # Human-readable: what changed from last round
    lineage: list[str] = field(default_factory=list)
    constraints_active: list[str] = field(default_factory=list)
    constraints_retired: list[str] = field(default_factory=list)
    technique_used: str = ""
    reference_file: str = ""             # Path to technique reference .md
    loop_id: str = ""
    round: int = 0
    goal_id: str = ""
    goal_text_hash: str = ""             # Auxiliary: SHA256 of normalized task text
    loop_objective: LoopObjective | None = None
    loop_health: LoopHealth | None = None
    task_alignment: TaskAlignment | None = None
    rolling_summary: RollingSummary | None = None   # v3.5: cross-round knowledge distillation
    suggested_next_task: str = ""
    plan_source: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str = ""


# ── Response schemas ──────────────────────────────────────────────────────────

@dataclass
class PromptCraftResponse:
    """Standard response: a complete, structured prompt ready for execution."""
    status: AgentStatus = AgentStatus.OK
    prompt: str | None = None                    # The full prompt text
    analysis: Analysis | None = None
    error: str | None = None                     # Only when status=error


@dataclass
class AgentLoopResult:
    """Unified result from one Agent Loop invocation.

    The main agent checks .status to decide next action:
      - OK      → execute the prompt
      - STALLED → read question_for_main_agent, ask user, call back with answer
      - ERROR   → handle failure
    """
    status: AgentStatus
    response: PromptCraftResponse | None = None


# ── Session state (Engine internal) ───────────────────────────────────────────

@dataclass
class SessionState:
    """Mutable state carried across Agent invocations within one session.

    Equivalent to query.ts's State object — reassigned as a whole on each iteration.
    """
    task_id: str
    call_count: int = 0
    quality_trend: list[int] = field(default_factory=list)    # Scores per iteration
    current_version: str = "v1"
    last_technique: str | None = None
    circuit_breaker_count: int = 0                            # Consecutive no-improvement
    feedback_buffer: list[dict[str, Any]] = field(default_factory=list)


# ── Serialisation helpers ─────────────────────────────────────────────────────

def to_dict(obj: Any) -> dict[str, Any]:
    """Convert any protocol dataclass to a JSON-serialisable dict."""
    def _convert(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, uuid.UUID):
            return str(value)
        if hasattr(value, "__dataclass_fields__"):
            return {k: _convert(v) for k, v in asdict(value).items()}
        if isinstance(value, dict):
            return {k: _convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_convert(v) for v in value]
        return value
    return _convert(asdict(obj))


# ── Factory helpers ───────────────────────────────────────────────────────────

def make_task_id(task_description: str) -> str:
    """Derive a kebab-case task_id from a task description."""
    import re
    slug = task_description.lower().strip()[:60]
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug or "unnamed-task"
