"""PromptCraft Agent — Circuit Breaker (Layer 5 of Execution Boundary).

Three-state machine inspired by Claude Code's denial tracking (3 consecutive
denials → degrade, 20 total → abort). Adapted for PromptCraft's threat model:
knowledge pollution and trust-chain abuse, not shell injection.

States:
  CLOSED     — normal operation, all tools allowed
  HALF_OPEN  — after consecutive denials, probing whether conditions improved
  OPEN       — tripped; all tool calls blocked until cooldown expires

Cf. Claude Code's src/utils/permissions/denialTracking.ts
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    HALF_OPEN = "HALF_OPEN"
    OPEN = "OPEN"


# ── Limits ──────────────────────────────────────────────────────────────────────

@dataclass
class BreakerLimits:
    max_consecutive_denials: int = 3       # Trip after 3 consecutive denials
    max_consecutive_low_quality: int = 5    # Signal ->break after 5 low-quality
    max_total_tool_calls: int = 100         # Hard cap per session
    max_vault_writes: int = 50              # Hard cap per session (shared with boundary.py)
    cooldown_seconds: float = 300.0         # 5-minute cooldown when OPEN


# ── State ───────────────────────────────────────────────────────────────────────

@dataclass
class CircuitBreakerState:
    consecutive_denials: int = 0
    consecutive_low_quality: int = 0
    total_tool_calls: int = 0
    total_vault_writes: int = 0
    total_denials: int = 0          # Session total (cf. Claude Code's maxTotal: 20)
    session_start: float = field(default_factory=time.monotonic)
    state: BreakerState = BreakerState.CLOSED
    last_state_change: float = field(default_factory=time.monotonic)


# ── Circuit Breaker ─────────────────────────────────────────────────────────────

class CircuitBreaker:
    """Three-state circuit breaker for PromptCraft tool execution.

    Usage in engine.py:
        breaker = CircuitBreaker()

        def invoke_build(self, request, hydrate_results):
            if not breaker.before_tool_call():
                return denied_response("Circuit breaker is OPEN")
            try:
                result = tool.call(request, self._ctx)
                breaker.after_success()
                return result
            except Exception:
                breaker.after_denial()
                raise
    """

    def __init__(self, limits: BreakerLimits | None = None) -> None:
        self.limits = limits or BreakerLimits()
        self._state = CircuitBreakerState()

    # ── Public API ──────────────────────────────────────────────────────────

    def before_tool_call(self) -> bool:
        """Call before every tool execution. Returns False if the tool must be
        blocked (breaker is OPEN and cooldown hasn't elapsed).

        Also enforces the hard total_tool_calls cap.
        """
        now = time.monotonic()

        if self._state.state == BreakerState.OPEN:
            elapsed = now - self._state.last_state_change
            if elapsed >= self.limits.cooldown_seconds:
                self._transition_to(BreakerState.HALF_OPEN, now)
            else:
                return False  # Still in cooldown

        if self._state.total_tool_calls >= self.limits.max_total_tool_calls:
            self._transition_to(BreakerState.OPEN, now)
            return False

        return True

    def after_success(self) -> None:
        """Call after a tool executes successfully.

        Resets consecutive_denials (cf. Claude Code: one success resets
        the consecutive counter — the model has adjusted its strategy).
        Does NOT reset total_denials.
        """
        self._state.consecutive_denials = 0
        self._state.total_tool_calls += 1

        if self._state.state == BreakerState.HALF_OPEN:
            self._transition_to(BreakerState.CLOSED)

    def after_denial(self) -> None:
        """Call after a tool is denied (by any guard layer).

        Tracks denials and trips the breaker if thresholds are exceeded.
        """
        self._state.consecutive_denials += 1
        self._state.total_denials += 1
        self._state.total_tool_calls += 1

        if self._state.consecutive_denials >= self.limits.max_consecutive_denials:
            self._transition_to(BreakerState.OPEN)

    def after_vault_write(self) -> None:
        """Track vault writes for rate limiting."""
        self._state.total_vault_writes += 1

    def after_low_quality(self) -> bool:
        """Track low-quality feedback. Returns True if ->break signal should fire.

        This is the LOW-QUALITY-ONLY stall detection (cf. health_report.py's
        stall detection: only low quality (<=3) counts as stall).
        """
        self._state.consecutive_low_quality += 1
        return self._state.consecutive_low_quality >= self.limits.max_consecutive_low_quality

    def reset_quality_stall(self) -> None:
        """Reset low-quality counter when a good-quality result arrives."""
        self._state.consecutive_low_quality = 0

    def can_write_vault(self) -> bool:
        """Check if vault write is allowed (rate limit)."""
        return self._state.total_vault_writes < self.limits.max_vault_writes

    def is_open(self) -> bool:
        return self._state.state == BreakerState.OPEN

    # ── Internal ────────────────────────────────────────────────────────────

    def _transition_to(self, target: BreakerState, now: float | None = None) -> None:
        if self._state.state == target:
            return
        self._state.state = target
        self._state.last_state_change = now or time.monotonic()

    # ── Debug / introspection ───────────────────────────────────────────────

    def summary(self) -> dict:
        return {
            "state": self._state.state.value,
            "consecutive_denials": self._state.consecutive_denials,
            "total_denials": self._state.total_denials,
            "total_tool_calls": self._state.total_tool_calls,
            "total_vault_writes": self._state.total_vault_writes,
            "consecutive_low_quality": self._state.consecutive_low_quality,
        }
