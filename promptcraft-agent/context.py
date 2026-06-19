"""PromptCraft Agent — EngineContext (shared context container).

Cf. Claude Code's context.ts: memoized getSystemContext / getUserContext.
EngineContext is the single source of truth for all data shared across
Tools within one Engine session. It replaces the ad-hoc ctx dict that
was previously passed between Engine and Tools.

Lifecycle rules:
  - hydrate_results (query): cached per session, invalidated when feedback written
  - hydrate_results (aggregate): never cached (always scans latest vault state)
  - overlay_config: cached per skill_name, invalidated when hydrate dirty
  - pattern_report: computed once per session unless feedback invalidates
  - skill_advice: computed once per session unless pattern_report updates
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from protocol import OverlayConfig, PatternReport, SkillAdvice


# ── Engine Metrics ────────────────────────────────────────────────────────────

@dataclass
class EngineMetrics:
    """Observability counters for silent/non-blocking operations.

    Silent failures (subprocess timeouts, vault write errors, cache misses)
    are tracked here so the HealthReport can surface degradation without
    breaking the fail-closed contract.

    All counters are monotonic within a session.
    """
    vault_write_errors: int = 0       # subprocess failures writing feedback
    vault_write_timeouts: int = 0     # subprocess timeouts
    vault_write_bytes: int = 0        # total bytes written to vault this session
    silent_analysis_errors: int = 0   # exceptions caught in maybe_silent_analyze
    subprocess_timeouts: int = 0      # any subprocess timeout (hydrate, checkpoint)
    hydrate_cache_misses: int = 0     # hydrate queries that missed the cache
    feedback_buffer_flushes: int = 0  # times the feedback buffer was flushed
    feedback_buffer_max_size: int = 0 # peak buffer size before flush
    session_start: float = 0.0        # monotonic timestamp

    FEEDBACK_FLUSH_INTERVAL = 5       # flush buffer every N feedback records

    def should_flush(self) -> bool:
        """True when the feedback buffer should be flushed to vault."""
        return (self.vault_write_errors + self.vault_write_timeouts +
                self.feedback_buffer_flushes * self.FEEDBACK_FLUSH_INTERVAL) == 0  # always flush if no errors
        # After errors accumulate, flush less aggressively


@dataclass
class EngineContext:
    """Per-session context shared across all Tools in one Engine session.

    Three layers:
      Layer 1 — Hydration: vault data, loaded per-session with cache control
      Layer 2 — Intermediate products: Tool outputs passed to downstream Tools
      Layer 3 — Accumulation: grows across invocations within the session
    """

    # ── Layer 1: Hydration ──────────────────────────────────────────────────

    hydrate_results: dict[str, Any] | None = None  # result of last hydrate.py call
    hydrate_mode: str = ""                          # "query" | "aggregate" | ""
    _hydrate_cache_key: str = ""                    # what query produced this cache
    _hydrate_dirty: bool = False                    # True = feedback written, cache stale

    # ── Layer 2: Intermediate products ──────────────────────────────────────

    overlay_config: OverlayConfig | None = None       # PersonalizationTool output
    pattern_report: PatternReport | None = None       # PatternAnalysisTool output
    skill_advice: SkillAdvice | None = None            # SkillAdvisorTool output
    proactive_signals: list[str] = field(default_factory=list)  # Proactive context hints

    # ── Layer 3: Accumulation ───────────────────────────────────────────────

    feedback_signals: list[dict[str, Any]] = field(default_factory=list)
    analysis_count: int = 0

    # ── Session identity ────────────────────────────────────────────────────

    skills_dir: str = "skills"

    # ── Cache control ───────────────────────────────────────────────────────

    def invalidate_hydrate(self) -> None:
        """Mark hydrate cache as dirty. Called after feedback is written to vault."""
        self._hydrate_dirty = True
        self._hydrate_cache_key = ""

    def is_hydrate_fresh(self, cache_key: str = "") -> bool:
        """Check if cached hydrate_results is still valid for the given key."""
        if self._hydrate_dirty:
            return False
        if self.hydrate_results is None:
            return False
        if cache_key and self._hydrate_cache_key != cache_key:
            return False
        return True

    def cache_hydrate(self, results: dict[str, Any], cache_key: str) -> None:
        """Store hydrate results and mark cache as clean."""
        self.hydrate_results = results
        self._hydrate_cache_key = cache_key
        self._hydrate_dirty = False
