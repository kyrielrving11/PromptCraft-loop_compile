"""PromptCraft-loop_compile — Engine (outer loop manager).

v3.5: 3-mode engine with vault-backed loop lineage persistence.
invoke_loop_compile (primary), invoke_build (internal), invoke_feedback.
Circuit breaker (_should_break) prevents infinite stall loops.
EngineMetrics tracks silent-failure counters for observability.

Lineage Dual-Write (v3.4):
  - Vault JSON: structured, searchable source of truth (via checkpoint.py --batch)
  - Markdown frontmatter: human-readable, git-friendly projection (.promptcraft/prompts/)
  - _hydrate_loop_context reads JSON first, falls back to scanning .md files
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol import (
    AgentLoopResult, AgentStatus, Analysis,
    PromptCraftRequest,
    PromptCraftResponse, SessionState,
    LoopCompileRequest, LoopCompileResponse, LoopRoundResult, LoopObjective,
    to_dict,
)
from builder import score_quality
from loop_compiler import compile_loop


def create_engine(skills_dir: str = "skills") -> "PromptCraftEngine":
    """Factory for creating a PromptCraftEngine instance."""
    return PromptCraftEngine(skills_dir=skills_dir)


# ── YAML Frontmatter (stdlib-only, for Markdown dual-write) ──────────────────

def _escape_yaml_string(s: str) -> str:
    """Quote a string for YAML if it contains special characters."""
    if not s:
        return '""'
    special = set(':#{}[]&*?|<>%=!%@`,\'"')  # ! is YAML tag; | is block scalar
    if not any(c in s for c in special) and not s.startswith(' ') and not s.endswith(' '):
        return s
    escaped = s.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _build_yaml_frontmatter(data: dict[str, Any], indent: int = 0) -> str:
    """Build a YAML frontmatter block from a flat dict with str/int/float/bool/list/dict values.

    Returns the YAML block as a string (without the `---` delimiters).
    """
    lines: list[str] = []
    pad = "  " * indent

    for key, value in data.items():
        if value is None:
            lines.append(f"{pad}{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{pad}{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{pad}{key}: {value}")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}:")
                for item in value:
                    lines.append(f"{pad}  - {_escape_yaml_string(str(item))}")
        elif isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}:")
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, list):
                        if not sub_value:
                            lines.append(f"{pad}  {sub_key}: []")
                        else:
                            lines.append(f"{pad}  {sub_key}:")
                            for item in sub_value:
                                lines.append(f"{pad}    - {_escape_yaml_string(str(item))}")
                    elif isinstance(sub_value, bool):
                        lines.append(f"{pad}  {sub_key}: {'true' if sub_value else 'false'}")
                    elif isinstance(sub_value, (int, float)):
                        lines.append(f"{pad}  {sub_key}: {sub_value}")
                    elif sub_value is None:
                        lines.append(f"{pad}  {sub_key}: null")
                    else:
                        lines.append(f"{pad}  {sub_key}: {_escape_yaml_string(str(sub_value))}")
        else:
            lines.append(f"{pad}{key}: {_escape_yaml_string(str(value))}")

    return "\n".join(lines)


def _parse_yaml_frontmatter(text: str) -> dict[str, Any] | None:
    """Parse YAML frontmatter from markdown text (delimited by ---).

    Minimal parser — handles the subset produced by _build_yaml_frontmatter:
    scalars (str/int/float/bool/null), lists, and nested dicts one level deep.
    Returns None if no frontmatter is found or parsing fails.
    """
    if not text.startswith("---\n"):
        return None

    # Handle empty frontmatter: "---\n---\n..."
    if text.startswith("---\n---\n", 0):
        return {}

    end_idx = text.find("\n---\n", 4)
    if end_idx == -1:
        # Try closing at end of text
        if text.endswith("\n---"):
            end_idx = len(text) - 4
        else:
            return None

    yaml_block = text[4:end_idx]
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_sub_key: str | None = None
    in_nested: str | None = None  # name of nested dict currently being parsed

    for line in yaml_block.split("\n"):
        if not line.strip():
            continue

        # Nested list item (4-space indent): "    - value"
        if line.startswith("    - ") and in_nested and current_sub_key:
            item = line[6:].strip()
            item = item.strip('"')
            nested = result.setdefault(in_nested, {})
            nested.setdefault(current_sub_key, []).append(item)
            continue

        # Top-level list item (2-space indent): "  - value"
        if line.startswith("  - ") and current_key and not line.startswith("    "):
            item = line[4:].strip()
            item = item.strip('"')
            result.setdefault(current_key, []).append(item)
            continue

        # Nested key (2-space indent): "  sub_key: value"
        if line.startswith("  ") and not line.startswith("    "):
            # Look for nested sub_key
            stripped = line[2:]
            if ":" in stripped:
                sub_key, _, sub_value = stripped.partition(":")
                sub_key = sub_key.strip()
                sub_value = sub_value.strip()
                current_sub_key = sub_key
                in_nested = current_key

                if sub_value == "":
                    # Could be start of nested list
                    continue
                sub_value = sub_value.strip('"')
                nested = result.setdefault(in_nested, {}) if in_nested else result
                nested[sub_key] = _coerce_yaml_scalar(sub_value)
            continue

        # Top-level key: value
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            current_key = key
            current_sub_key = None
            in_nested = None

            if value == "":
                # Could be start of a list or nested dict
                continue
            result[key] = _coerce_yaml_scalar(value.strip('"'))

    return result if result else None


def _coerce_yaml_scalar(value: str) -> Any:
    """Coerce a YAML scalar string to its Python type."""
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "None", "~"):
        return None
    try:
        if "." in value or "e" in value.lower():
            return float(value)
        return int(value)
    except ValueError:
        return value


def _lineage_dir_name(loop_id: str) -> str:
    """Convert loop_id to a filesystem-safe directory name.

    Replaces ':' with '-' since colons are invalid on Windows.
    E.g., 'audit-erc20' stays 'audit-erc20'; 'loop:smoke' becomes 'loop-smoke'.
    """
    return loop_id.replace(":", "-")


def _write_lineage_md(
    loop_id: str,
    round_num: int,
    goal_id: str,
    goal_text_hash: str,
    recompile_level: str,
    constraints_active: list[str],
    task: str,
    prompt_text: str,
    technique_used: str = "",
    loop_objective: dict[str, Any] | None = None,
    success: bool = True,
    quality_score: int = 0,
    output_summary: str = "",              # v3.5: previous round execution summary
    constraint_violations: list[str] | None = None,  # v3.5: previous round violations
) -> str | None:
    """Write loop lineage as a Markdown file with YAML frontmatter.

    Dual-write companion to _persist_loop_lineage's JSON vault write.
    The .md file is human-readable, git-friendly, and serves as a fallback
    read path when the JSON vault is unavailable.

    Returns the relative path to the written .md file, or None on failure.
    """
    prompts_dir = Path(".promptcraft/prompts")
    lineage_dir = prompts_dir / _lineage_dir_name(loop_id)
    md_path = lineage_dir / f"r{round_num}.md"

    try:
        lineage_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    # ── Build frontmatter ──
    fm_data: dict[str, Any] = {
        "loop_id": loop_id,
        "round": round_num,
        "goal_id": goal_id,
        "goal_text_hash": goal_text_hash,
        "recompile_level": recompile_level,
        "quality_score": quality_score,
        "constraints_active": constraints_active,
        "task": task,
        "success": success,
        "technique_used": technique_used,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if loop_objective:
        fm_data["loop_objective"] = loop_objective
    if output_summary:
        fm_data["output_summary"] = output_summary
    if constraint_violations:
        fm_data["constraint_violations"] = constraint_violations

    frontmatter = _build_yaml_frontmatter(fm_data)

    # ── Build body ──
    body_lines = [
        f"# Loop: {loop_id} — Round {round_num}",
        "",
        f"**Goal**: {task}",
        f"**Goal ID**: `{goal_id}`",
        f"**Recompile Level**: {recompile_level.upper()}",
        f"**Technique**: {technique_used or 'n/a'}",
        f"**Quality Score**: {quality_score}",
        "",
        "## Compiled Prompt",
        "",
        prompt_text,
    ]
    body = "\n".join(body_lines)

    try:
        with md_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write("---\n")
            f.write(frontmatter)
            f.write("\n---\n\n")
            f.write(body)
            f.write("\n")
    except OSError:
        return None

    return str(md_path.as_posix())


def _read_lineage_md(loop_id: str, round_num: int) -> dict[str, Any] | None:
    """Read loop lineage from a Markdown file (fallback when JSON vault unavailable).

    Parses the YAML frontmatter and extracts the prompt body.
    Returns a dict compatible with _hydrate_loop_context's result format,
    or None if the file doesn't exist or is unparseable.
    """
    md_path = Path(f".promptcraft/prompts/{_lineage_dir_name(loop_id)}/r{round_num}.md")
    if not md_path.exists():
        return None

    try:
        with md_path.open("r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    fm = _parse_yaml_frontmatter(text)
    if fm is None:
        return None

    # Extract body (everything after the closing ---)
    end_fm = text.find("\n---\n", 4)
    body = text[end_fm + 5:].strip() if end_fm != -1 else ""

    return {
        "task_id": f"loop:{loop_id}:r{round_num}",
        "user_intent": fm.get("task", ""),
        "loop_id": fm.get("loop_id", loop_id),
        "loop_lineage": {
            "loop_id": fm.get("loop_id", loop_id),
            "round": fm.get("round", round_num),
            "goal_id": fm.get("goal_id", ""),
            "goal_text_hash": fm.get("goal_text_hash", ""),
            "recompile_level": fm.get("recompile_level", "l2"),
            "quality_score": fm.get("quality_score", 0),
            "constraints_active": fm.get("constraints_active", []),
            "task": fm.get("task", ""),
            "success": fm.get("success", True),
            "technique_used": fm.get("technique_used", ""),
        },
        "loop_objective": fm.get("loop_objective"),
        "task": fm.get("task", ""),
        "success": fm.get("success", True),
        "quality_score": fm.get("quality_score", 0),
        "technique_used": fm.get("technique_used", ""),
        "output_summary": fm.get("output_summary", ""),           # v3.5
        "constraint_violations": fm.get("constraint_violations", []),  # v3.5
        "full_prompt": body,
        "source": "markdown",
    }


def _scan_lineage_md(loop_id: str) -> list[dict[str, Any]]:
    """Scan .promptcraft/prompts/loop:{loop_id}/ for all round .md files.

    Returns a list of parsed lineage entries, sorted by round descending.
    Each entry is compatible with _hydrate_loop_context's result format.
    """
    lineage_dir = Path(f".promptcraft/prompts/{_lineage_dir_name(loop_id)}")
    if not lineage_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for md_file in sorted(lineage_dir.glob("r*.md")):
        # Extract round number from filename: r3.md → 3
        name = md_file.stem  # e.g., "r3"
        if not name.startswith("r"):
            continue
        try:
            round_num = int(name[1:])
        except ValueError:
            continue

        entry = _read_lineage_md(loop_id, round_num)
        if entry:
            results.append(entry)

    # Sort by round descending
    results.sort(key=lambda e: e.get("loop_lineage", {}).get("round", 0), reverse=True)
    return results


# ── Engine Metrics ────────────────────────────────────────────────────────────

@dataclass
class EngineMetrics:
    """Observability counters for silent/non-blocking operations.

    Silent failures (subprocess timeouts, vault write errors, cache misses)
    are tracked here so the HealthReport can surface degradation without
    breaking the fail-closed contract.

    All counters are monotonic within a session.
    """
    vault_write_errors: int = 0
    vault_write_timeouts: int = 0
    vault_write_bytes: int = 0
    silent_analysis_errors: int = 0
    subprocess_timeouts: int = 0
    hydrate_cache_misses: int = 0
    feedback_buffer_flushes: int = 0
    feedback_buffer_max_size: int = 0
    session_start: float = 0.0

    FEEDBACK_FLUSH_INTERVAL = 5


# ── Engine ─────────────────────────────────────────────────────────────────────

@dataclass
class PromptCraftEngine:
    """Manages the outer Agent Loop — iteration lifecycle, quality tracking,
    and circuit breaker decisions.

    One Engine instance per session. Each `invoke()` call represents one
    wake-up of the PromptCraft Agent by the main agent.

    v3: Tools are registered in priority order — Personalization checks first
    (Skill overlay), then Feedback/Review, and PromptBuild is the fallback.
    """

    skills_dir: str = "skills"
    state: SessionState | None = None    # None until first invocation

    # De-duplication cache (prevents recording the same constraint twice)
    _seen_constraints: set[str] = field(default_factory=set)

    # Circuit breaker — Layer 5 of the Execution Boundary

    # Tool registry — built lazily on first invoke

    # Paths for vault persistence (resolved relative to the project root)
    _checkpoint_script: Path | None = field(default=None, repr=False)

    # Session metrics — observability counters for silent failures
    _metrics: EngineMetrics | None = field(default=None, repr=False)

    # Last task processed — used for lightweight vault probing in maybe_silent_analyze
    _last_task: str | None = field(default=None, repr=False)


    def _ensure_init(self, request: PromptCraftRequest) -> None:
        """Lazy-init session state and metrics on first invocation."""
        if self.state is None:
            from protocol import make_task_id
            self.state = SessionState(
                task_id=request.task_id or make_task_id(request.task),
            )
        if self._metrics is None:
            import time as _time
            self._metrics = EngineMetrics(session_start=_time.monotonic())

    def _resolve_checkpoint_script(self) -> bool:
        """Resolve the path to checkpoint.py, caching it on the engine instance.

        Shared by _flush_feedback_buffer and _persist_loop_lineage.
        Returns True if the script was found, False otherwise.
        """
        if self._checkpoint_script is not None:
            return True

        candidate = Path("skills/prompt-memory/scripts/checkpoint.py")
        if candidate.exists():
            self._checkpoint_script = candidate.resolve()
            return True

        engine_dir = Path(__file__).resolve().parent.parent
        candidate = engine_dir / "skills/prompt-memory/scripts/checkpoint.py"
        if candidate.exists():
            self._checkpoint_script = candidate
            return True

        if self._metrics:
            self._metrics.vault_write_errors += 1
        return False

    def _persist_feedback_to_vault(self, signal: dict[str, Any]) -> None:
        """Write a feedback record to vault via checkpoint.py subprocess.

        Buffers writes when the feedback pipeline is active — flushes every
        EngineMetrics.FEEDBACK_FLUSH_INTERVAL records or on session end.
        Tracks errors in EngineMetrics for HealthReport visibility.

        Failure is non-blocking: if the subprocess fails, we track the error
        in metrics and continue. The in-memory buffer still works for
        same-session analysis.
        """
        if self._metrics is None:
            import time as _time
            self._metrics = EngineMetrics(session_start=_time.monotonic())

        # ── Buffer the signal for batched write ──
        if not hasattr(self, '_feedback_write_buffer'):
            self._feedback_write_buffer: list[dict[str, Any]] = []
        self._feedback_write_buffer.append(signal)

        buf_len = len(self._feedback_write_buffer)
        if buf_len > self._metrics.feedback_buffer_max_size:
            self._metrics.feedback_buffer_max_size = buf_len

        # Flush when buffer reaches threshold
        if buf_len >= self._metrics.FEEDBACK_FLUSH_INTERVAL:
            self._flush_feedback_buffer()
        # Note: remaining buffer entries are flushed by maybe_silent_analyze()
        # at session end, ensuring no data loss.

    def _flush_feedback_buffer(self) -> int:
        """Flush buffered feedback records to vault in a single subprocess call.

        Builds proper checkpoint payloads from buffer signals and writes them
        via NDJSON batch mode. Returns the number of records successfully persisted.
        """
        if not hasattr(self, '_feedback_write_buffer') or not self._feedback_write_buffer:
            return 0

        records = self._feedback_write_buffer[:]
        self._feedback_write_buffer.clear()
        if self._metrics:
            self._metrics.feedback_buffer_flushes += 1

        # Resolve checkpoint script path (shared — cached on engine)
        if not self._resolve_checkpoint_script():
            if self._metrics:
                self._metrics.vault_write_errors += len(records)
            return 0

        # Build payload for each signal record
        payloads: list[str] = []
        for signal in records:
            payload = {
                "task_id": signal.get("task_id", "feedback"),
                "user_intent": signal.get("task_type", "unknown"),
                "task_type": signal.get("task_type", ""),
                "quality_score": signal.get("quality_score", 0),
                "overlay_used": signal.get("overlay_used", []),
                "skill_used": signal.get("skill_used", ""),
                "execution_feedback": json.dumps({
                    "status": "success" if signal.get("quality_score", 0) >= 3 else "partial",
                    "quality_score": signal.get("quality_score", 0),
                    "constraint_compliance": {
                        "all_hard_constraints_met": not signal.get("violations"),
                        "violations": signal.get("violations", []),
                    },
                    "output_summary": signal.get("task_type", ""),
                    "improvement_notes": signal.get("manual_fixes", ""),
                }),
                "tags": [signal.get("skill_used", "")] if signal.get("skill_used") else [],
            }
            payloads.append(json.dumps(payload))

        ndjson_input = "\n".join(payloads)

        try:
            proc = subprocess.run(
                [
                    sys.executable, str(self._checkpoint_script),
                    "--batch",
                ],
                input=ndjson_input,
                capture_output=True,
                text=True,
                timeout=10 + len(records) * 2,  # 10s base + 2s per record
            )
            if proc.returncode != 0:
                if self._metrics:
                    self._metrics.vault_write_errors += len(records)
            else:
                total_bytes = len(ndjson_input.encode("utf-8"))
                if self._metrics:
                    self._metrics.vault_write_bytes += total_bytes
        except subprocess.TimeoutExpired:
            if self._metrics:
                self._metrics.vault_write_timeouts += len(records)
                self._metrics.subprocess_timeouts += 1
        except (OSError, ValueError):
            if self._metrics:
                self._metrics.vault_write_errors += len(records)

        return len(records)

    def _persist_loop_lineage(
        self,
        response: "LoopCompileResponse",
        request: "LoopCompileRequest",
    ) -> bool:
        """Persist loop_compile lineage to vault after each round.

        Dual-write (v3.4):
          1. JSON vault via checkpoint.py — structured, searchable source of truth
          2. Markdown frontmatter via _write_lineage_md — human-readable, git-friendly

        Failure is non-blocking — tracked in EngineMetrics.

        Returns True if JSON vault write succeeded, False otherwise.
        (Markdown write failure is silent — tracked via metrics.)
        """
        if self._metrics is None:
            import time as _time
            self._metrics = EngineMetrics(session_start=_time.monotonic())

        # Resolve checkpoint script path (shared — cached on engine)
        if not self._resolve_checkpoint_script():
            return False

        loop_obj_dict = None
        if response.loop_objective is not None:
            loop_obj_dict = to_dict(response.loop_objective)

        # Structured lineage dict — matches get_previous_round() read format
        structured_lineage = {
            "loop_id": response.loop_id,
            "round": response.round,
            "goal_id": response.goal_id,
            "goal_text_hash": response.goal_text_hash,
            "recompile_level": response.recompile_level,
            "quality_score": 0,  # Not known at compile time; filled by feedback
            "constraints_active": response.constraints_active,
            "task": request.task,
            "success": True,  # Optimistic; feedback mode corrects this
            "technique_used": response.technique_used,  # v3.5: for adaptive routing
        }

        # ── v3.5: Carry previous round's execution data for cross-round analysis ──
        last_output_summary = ""
        last_violations: list[str] = []
        if request.last_round_result is not None:
            last_output_summary = request.last_round_result.output_summary or ""
            last_violations = request.last_round_result.constraint_violations or []

        payload = {
            "task_id": f"loop:{response.loop_id}:r{response.round}",
            "user_intent": f"loop_compile round {response.round} — {response.goal_id}",
            "loop_id": response.loop_id,
            "loop_lineage": structured_lineage,
            "loop_objective": loop_obj_dict,
            "task": request.task,
            "task_type": "loop_lineage",
            "quality_score": 0,
            "skill_used": response.technique_used,
            "technique_used": response.technique_used,  # v3.5: normalized field name
            "output_summary": last_output_summary,       # v3.5: prev round execution
            "constraint_violations": last_violations,     # v3.5: prev round violations
            "tags": [response.loop_id, response.recompile_level, response.goal_id],
        }

        ndjson_input = json.dumps(payload)

        # ── 1. JSON vault write (primary) ──
        vault_ok = False
        try:
            proc = subprocess.run(
                [
                    sys.executable, str(self._checkpoint_script),
                    "--batch",
                ],
                input=ndjson_input + "\n",
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                if self._metrics:
                    self._metrics.vault_write_errors += 1
            else:
                if self._metrics:
                    self._metrics.vault_write_bytes += len(ndjson_input.encode("utf-8"))
                vault_ok = True
        except subprocess.TimeoutExpired:
            if self._metrics:
                self._metrics.vault_write_timeouts += 1
                self._metrics.subprocess_timeouts += 1
        except (OSError, ValueError):
            if self._metrics:
                self._metrics.vault_write_errors += 1

        # ── 2. Markdown frontmatter write (secondary, non-blocking) ──
        try:
            prompt_text = response.prompt or ""
            md_path = _write_lineage_md(
                loop_id=response.loop_id,
                round_num=response.round,
                goal_id=response.goal_id,
                goal_text_hash=response.goal_text_hash,
                recompile_level=response.recompile_level,
                constraints_active=response.constraints_active,
                task=request.task,
                prompt_text=prompt_text,
                technique_used=response.technique_used,
                loop_objective=loop_obj_dict,
                success=True,
                quality_score=0,
                output_summary=last_output_summary,
                constraint_violations=last_violations,
            )
            if md_path and self._metrics:
                self._metrics.vault_write_bytes += len(prompt_text.encode("utf-8"))
        except (OSError, ValueError):
            if self._metrics:
                self._metrics.vault_write_errors += 1

        return vault_ok

    def _hydrate_loop_context(self, loop_id: str) -> dict[str, Any] | None:
        """Find prior rounds for the given loop_id from vault.

        Reads the project vault directly (no semantic search — needs exact
        loop_id prefix match). Falls back to scanning .promptcraft/prompts/
        for Markdown frontmatter files if the JSON vault is unavailable or
        has no matching entries.

        Enriches each result with 'full_prompt' — the compiled prompt text
        from the Markdown file — so L0 can reuse the cached prompt.

        Returns a dict with 'results' key containing matching entries,
        compatible with get_previous_round() interface.
        """
        vault_path = Path(".promptcraft/prompt_vault.json")
        results: list[dict[str, Any]] = []

        if vault_path.exists():
            try:
                with vault_path.open("r", encoding="utf-8") as f:
                    vault = json.load(f)
            except (json.JSONDecodeError, OSError):
                vault = None
        else:
            vault = None

        if vault is not None:
            prefix = f"loop:{loop_id}:r"
            results = [
                e for e in vault.get("entries", [])
                if isinstance(e, dict) and str(e.get("task_id", "")).startswith(prefix)
            ]

            # ── v3.5.1: Merge feedback quality scores into lineage entries ──
            feedback_prefix = f"loop:{loop_id}:r"
            feedback_entries = [
                e for e in vault.get("entries", [])
                if isinstance(e, dict)
                and str(e.get("task_id", "")).startswith(feedback_prefix)
                and str(e.get("task_id", "")).endswith(":feedback")
            ]
            # Build a round→quality_score map from feedback entries
            fb_quality: dict[int, int] = {}
            for fe in feedback_entries:
                # task_id format: loop:{loop_id}:r{N}:feedback
                tid = str(fe.get("task_id", ""))
                try:
                    # Extract round number: "loop:id:r3:feedback" → 3
                    parts = tid.split(":r")
                    if len(parts) >= 2:
                        round_str = parts[-1].split(":")[0]
                        fb_round = int(round_str)
                        fb_score = fe.get("quality_score", 0)
                        if fb_score > 0:
                            # Take the most recent (highest) quality score if multiple
                            fb_quality[fb_round] = max(fb_quality.get(fb_round, 0), fb_score)
                except (ValueError, IndexError):
                    pass

            # Apply feedback quality scores to lineage entries
            for entry in results:
                lineage = entry.get("loop_lineage") or entry.get("lineage") or {}
                rnd = lineage.get("round")
                if rnd and rnd in fb_quality:
                    lineage["quality_score"] = fb_quality[rnd]
                    entry["quality_score"] = fb_quality[rnd]

        # ── Fallback: scan Markdown files if JSON vault had no matches ──
        if not results:
            md_results = _scan_lineage_md(loop_id)
            if md_results:
                results = md_results

        # ── v3.5: Normalize technique_used field (vault uses skill_used, markdown uses technique_used) ──
        for entry in results:
            if not entry.get("technique_used"):
                entry["technique_used"] = entry.get("skill_used", "")
            # Also normalise output_summary / constraint_violations from loop_lineage
            lineage = entry.get("loop_lineage") or entry.get("lineage") or {}
            if not entry.get("output_summary"):
                entry["output_summary"] = lineage.get("output_summary", "")
            if not entry.get("constraint_violations"):
                entry["constraint_violations"] = lineage.get("constraint_violations", [])

        # ── Enrich: attach full prompt text from Markdown for L0 cache reuse ──
        for entry in results:
            if "full_prompt" in entry and entry["full_prompt"]:
                continue  # Already has prompt (from markdown fallback)
            lineage = entry.get("loop_lineage") or entry.get("lineage") or {}
            round_num = lineage.get("round")
            if round_num:
                md_entry = _read_lineage_md(loop_id, round_num)
                if md_entry and md_entry.get("full_prompt"):
                    entry["full_prompt"] = md_entry["full_prompt"]

        if not results:
            return None

        return {"results": results, "global_entries": []}





    def invoke_build(
        self,
        request: PromptCraftRequest,
        hydrate_results: dict[str, Any] | None = None,
    ) -> AgentLoopResult:
        """Build mode: minimal prompt generation using builder.route_technique.

        Internal implementation — called by loop_compile L2 path. Not an
        externally-facing mode in v3.4.
        """
        self._ensure_init(request)
        self._last_task = request.task

        from builder import route_technique as _route
        analysis = _route(request.task)
        technique = analysis.technique
        rationale = analysis.rationale

        prompt_sections = [
            f"## PromptCraft Build",
            f"**Technique**: {technique}",
            f"**Rationale**: {rationale}",
            "",
            f"### Task",
            request.task,
            "",
            f"### Instructions",
            f"Apply the **{technique}** technique to complete the task above.",
            "Respect all hard constraints and provide verifiable output.",
        ]

        return AgentLoopResult(
            status=AgentStatus.OK,
            response=PromptCraftResponse(
                status=AgentStatus.OK,
                prompt="\n".join(prompt_sections),
                analysis=analysis,
            ),
        )

    # ── Feedback (public) ───────────────────────────────────────────────────

    def invoke_feedback(
        self,
        request: PromptCraftRequest,
        hydrate_results: dict[str, Any] | None = None,
    ) -> AgentLoopResult:
        """Feedback mode: collect execution feedback and persist to vault.

        Extracts explicit feedback signals from the request and implicit
        signals from context, then runs the feedback pipeline: quality
        scoring → buffer → vault persistence → circuit breaker check.
        """
        self._ensure_init(request)
        self._last_task = request.task

        # ── Validate: feedback payload is required ──
        fb = request.feedback
        if fb is None:
            return AgentLoopResult(
                status=AgentStatus.ERROR,
                response=PromptCraftResponse(
                    status=AgentStatus.ERROR,
                    error="Feedback mode requires a feedback payload.",
                ),
            )

        # ── Extract explicit feedback signal ──
        signals: list[dict[str, Any]] = []
        if fb is not None:
            success = fb.success if hasattr(fb, "success") else (
                fb.get("success") if isinstance(fb, dict) else None
            )
            violations = (
                fb.constraint_violations if hasattr(fb, "constraint_violations")
                else (fb.get("constraint_violations", []) if isinstance(fb, dict) else [])
            )
            fixes = (
                fb.manual_fixes_needed if hasattr(fb, "manual_fixes_needed")
                else (fb.get("manual_fixes_needed", "") if isinstance(fb, dict) else "")
            )
            signals.append({
                "signal_type": "explicit",
                "description": (
                    f"success={success}, violations={violations}, fixes={fixes}"
                ),
                "task_type": request.task[:80] if request.task else "",
                "skill_used": getattr(request, "skill_name", None),
                "overlay_used": getattr(request, "overlay_used", []),
            })

        if not signals:
            return AgentLoopResult(
                status=AgentStatus.ERROR,
                response=PromptCraftResponse(
                    status=AgentStatus.ERROR,
                    error="No feedback signals to collect.",
                ),
            )

        # ── Score quality and persist ──
        # Normalise success from dict or ExecutionFeedback dataclass
        if fb is not None:
            if hasattr(fb, "success"):
                fb_success = fb.success
            elif isinstance(fb, dict):
                fb_success = fb.get("success", True)
            else:
                fb_success = True
        else:
            fb_success = True
        quality = score_quality({
            "success": fb_success,
            "constraint_violations": violations if fb else [],
        })

        # ── v3.5.1: Loop-aware task_id for feedback→lineage backfill ──
        loop_id = getattr(request, 'loop_id', None)
        loop_round = getattr(request, 'round', None)
        if loop_id and loop_round:
            task_id = f"loop:{loop_id}:r{loop_round}:feedback"
        else:
            task_id = getattr(request, 'task_id', None) or (request.task[:60] if request.task else "feedback")

        signal = {
            "task_id": task_id,
            "task_type": request.task[:80] if request.task else "",
            "quality_score": quality,
            "skill_used": getattr(request, "skill_name", ""),
            "violations": violations if fb else [],
            "manual_fixes": fixes if fb else [],
            "loop_id": loop_id,       # v3.5.1: for cross-reference
            "round": loop_round,       # v3.5.1: for cross-reference
        }
        self._persist_feedback_to_vault(signal)

        # ── v3.5.1: Flush immediately so next compile cycle sees quality scores ──
        self._flush_feedback_buffer()

        # Count feedback in state
        self.state.call_count += 1
        self.state.quality_trend.append(quality)
        if len(self.state.quality_trend) > 20:
            self.state.quality_trend = self.state.quality_trend[-20:]

        # Update circuit breaker on each feedback cycle
        if self._should_break():
            self.state.circuit_breaker_count += 1
        else:
            self.state.circuit_breaker_count = 0

        return AgentLoopResult(
            status=AgentStatus.OK,
            response=PromptCraftResponse(
                status=AgentStatus.OK,
                prompt=f"## Feedback Recorded\n\nQuality Score: {quality}/5\nSignals: {len(signals)}",
                analysis=Analysis(technique="feedback", rationale=f"quality={quality}", independence="n/a", cognitive_load="low"),
            ),
        )

    # ── Loop Compile (public, v3.3) ──────────────────────────────────────────

    def invoke_loop_compile(
        self,
        request: PromptCraftRequest,
        hydrate_results: dict[str, Any] | None = None,
    ) -> AgentLoopResult:
        """Loop Compile mode: per-iteration prompt compiler.

        Converts a PromptCraftRequest into a LoopCompileRequest, delegates
        to the pure-function loop_compiler.compile_loop(), and wraps the
        result as an AgentLoopResult for protocol compatibility.
        """
        self._ensure_init(request)
        self._last_task = request.task

        # ── Build LoopCompileRequest from PromptCraftRequest ──
        lcr = LoopCompileRequest(
            loop_id=getattr(request, "loop_id", "") or request.task_id or "",
            round=getattr(request, "round", 1),
            goal_id=getattr(request, "goal_id", ""),
            task=request.task,
            domain=getattr(request, "domain", ""),
            next_task_proposal=getattr(request, "next_task_proposal", ""),
            plan_source=getattr(request, "plan_source", None),
            constraints_from_plan=getattr(request, "constraints_from_plan", []),
            new_since_last_round=getattr(request, "new_since_last_round", ""),
            force_level=getattr(request, "force_level", "auto"),
            health_check_interval=getattr(request, "health_check_interval", 1),
        )

        # Convert last_round_result if present (dict from JSON or dataclass)
        last_rr = getattr(request, "last_round_result", None)
        if last_rr is not None:
            if isinstance(last_rr, dict):
                lcr.last_round_result = LoopRoundResult(
                    round=last_rr.get("round", 0),
                    success=last_rr.get("success", False),
                    output_summary=last_rr.get("output_summary", ""),
                    constraint_violations=last_rr.get("constraint_violations", []),
                    manual_fixes_needed=last_rr.get("manual_fixes_needed", ""),
                    quality_score=last_rr.get("quality_score", 0),
                )
            elif hasattr(last_rr, "round"):
                lcr.last_round_result = LoopRoundResult(
                    round=getattr(last_rr, "round", 0),
                    success=getattr(last_rr, "success", False),
                    output_summary=getattr(last_rr, "output_summary", ""),
                    constraint_violations=getattr(last_rr, "constraint_violations", []),
                    manual_fixes_needed=getattr(last_rr, "manual_fixes_needed", ""),
                    quality_score=getattr(last_rr, "quality_score", 0),
                )

        # Convert loop_objective if present
        lo = getattr(request, "loop_objective", None)
        if lo is not None:
            if isinstance(lo, dict):
                lcr.loop_objective = LoopObjective(
                    objective=lo.get("objective", ""),
                    success_criteria=lo.get("success_criteria", []),
                    hard_constraints=lo.get("hard_constraints", []),
                    created_at_round=lo.get("created_at_round", 1),
                    loop_id=lo.get("loop_id", ""),
                )
            elif hasattr(lo, "objective"):
                lcr.loop_objective = lo

        # ── Hydrate vault context for cross-round memory (v3.4) ──
        # If the caller didn't provide hydrate_results, try to load from vault
        # so get_previous_round() can find prior rounds.
        if hydrate_results is None and lcr.loop_id and lcr.round > 1:
            hydrate_results = self._hydrate_loop_context(lcr.loop_id)

        # ── Delegate to pure-function compiler ──
        try:
            response = compile_loop(lcr, hydrate_results)
        except Exception as exc:
            return AgentLoopResult(
                status=AgentStatus.ERROR,
                response=PromptCraftResponse(
                    status=AgentStatus.ERROR,
                    error=f"loop_compile failed: {exc}",
                ),
            )

        # ── Build prompt text from response ──
        prompt_lines = [
            f"## PromptCraft Loop Compile — Round {response.round}",
            f"**Recompile Level**: {response.recompile_level.upper()}",
            f"**Loop ID**: {response.loop_id}",
            f"**Goal ID**: {response.goal_id}",
            "",
            response.prompt,
        ]

        if response.warnings:
            prompt_lines.append("")
            prompt_lines.append("### Warnings")
            for w in response.warnings:
                prompt_lines.append(f"- ⚠️ {w}")

        if response.loop_health:
            h = response.loop_health
            prompt_lines.append("")
            prompt_lines.append("### Loop Health")
            prompt_lines.append(f"- Goal Alignment: {h.goal_alignment:.2f}")
            prompt_lines.append(f"- Constraint Integrity: {h.constraint_integrity:.2f}")
            prompt_lines.append(f"- Task Continuity: {h.task_continuity:.2f}")
            prompt_lines.append(f"- Drift Detected: {h.drift_detected}")
            prompt_lines.append(f"- Strategy Stability: {h.strategy_stability}")

        if response.task_alignment and response.task_alignment.escalation != "none":
            prompt_lines.append("")
            prompt_lines.append("### Task Alignment Advisory")
            prompt_lines.append(f"- Score: {response.task_alignment.alignment_score:.2f}")
            prompt_lines.append(f"- Escalation: {response.task_alignment.escalation}")
            prompt_lines.append(f"- {response.task_alignment.warning}")

        if response.suggested_next_task:
            prompt_lines.append("")
            prompt_lines.append(f"### Suggested Next Task\n{response.suggested_next_task}")

        # ── Persist lineage to vault for cross-round memory (v3.4) ──
        self._persist_loop_lineage(response, lcr)

        return AgentLoopResult(
            status=AgentStatus.OK,
            response=PromptCraftResponse(
                status=AgentStatus.OK,
                prompt="\n".join(prompt_lines),
                analysis=Analysis(
                    technique=response.technique_used,
                    rationale=f"Recompile level: {response.recompile_level}",
                    independence="n/a",
                    cognitive_load=(
                        "low" if response.recompile_level == "l0"
                        else "medium" if response.recompile_level == "l1"
                        else "high"
                    ),
                    reference_file=getattr(response, 'reference_file', ''),
                ),
            ),
        )

    # ── Review handler ─────────────────────────────────────────────────────

    def _handle_review(
        self,
        request: PromptCraftRequest,
        hydrate_results: dict[str, Any] | None,
    ) -> AgentLoopResult:
        """Review mode: audit an existing prompt for quality issues."""
        # Review mode loads the prompt from vault (via hydrate) and checks
        # structure, constraint compliance, and technique appropriateness.
        issues: list[str] = []
        if hydrate_results is None:
            return AgentLoopResult(
                status=AgentStatus.ERROR,
                response=PromptCraftResponse(
                    status=AgentStatus.ERROR,
                    error="Review mode requires hydrate_results (prompt to review).",
                ),
            )

        results = hydrate_results.get("results", [])
        if not results:
            return AgentLoopResult(
                status=AgentStatus.ERROR,
                response=PromptCraftResponse(
                    status=AgentStatus.ERROR,
                    error="No matching prompt found in vault to review.",
                ),
            )

        prompt_data = results[0]
        full_text = prompt_data.get("full_prompt", "")

        # Structural checks
        required_sections = ["角色", "任务", "输入", "输出格式", "硬约束", "生成要求"]
        for section in required_sections:
            if section not in full_text:
                issues.append(f"Missing section: {section}")

        # Constraint check
        global_entries = hydrate_results.get("global_entries", [])
        for entry in global_entries:
            for c in entry.get("hard_constraints_added", []):
                if c not in full_text:
                    issues.append(f"GLOBAL constraint not reflected: {c}")

        review_report = "\n".join(f"- {i}" for i in issues) if issues else "All checks passed."
        return AgentLoopResult(
            status=AgentStatus.OK,
            response=PromptCraftResponse(
                status=AgentStatus.OK,
                prompt=f"## Review Report\n\n{review_report}\n\n---\n\n{full_text}",
                analysis=None,
                metadata=None,
            ),
        )

    # ── Circuit breaker ─────────────────────────────────────────────────────

    MAX_CIRCUIT_BREAKER = 3  # Cf. Claude Code's MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES

    def _should_break(self) -> bool:
        """Pure read — check if the circuit breaker should trip.

        Trip condition: last MAX_CIRCUIT_BREAKER iterations show no quality
        improvement (trend is flat or declining). This is a pure function —
        it does NOT mutate state. The caller (invoke_feedback) is responsible
        for updating circuit_breaker_count.
        """
        if len(self.state.quality_trend) < self.MAX_CIRCUIT_BREAKER:
            return False

        recent = self.state.quality_trend[-self.MAX_CIRCUIT_BREAKER:]
        # Non-increasing sequence: each element >= the next
        return all(
            recent[i] >= recent[i + 1] for i in range(len(recent) - 1)
        )

