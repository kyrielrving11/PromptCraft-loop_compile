"""PromptCraft Agent — Tool base interface.

Every Tool in PromptCraft implements this contract. Cf. Claude Code's
Tool<Input, Output, P> generic — but simplified: we don't need streaming
progress, concurrent execution, or React rendering. PromptCraft tools
are linear, deterministic pipelines.

Design principles (from Claude Code study):
  - Fail-closed defaults: is_applicable() returns False by default.
  - Error as data: ToolResult can carry error state, never raises.
  - Self-describing: each tool provides its own prompt() for system prompt injection.
  - Tool-level safety: each tool declares safety attributes + implements
    check_permissions() — cf. Claude Code's Layer 5 (Tool-level Safety).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from protocol import ToolPermission


# ── Tool Result ─────────────────────────────────────────────────────────────────

@dataclass
class ToolResult:
    """Unified return type for all tools. Cf. Claude Code's ToolResult<T>.

    A tool either succeeds (data is set) or fails (error is set).
    It may optionally produce vault entries to be checkpointed.
    """
    data: dict[str, Any] | None = None
    error: str | None = None
    vault_entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


def tool_error(message: str) -> ToolResult:
    """Factory for error results — one line, no ceremony."""
    return ToolResult(error=message)


def tool_ok(data: dict[str, Any] | None = None, **kwargs: Any) -> ToolResult:
    """Factory for success results."""
    merged = (data or {}) | kwargs
    return ToolResult(data=merged)


# ── Tool Base ──────────────────────────────────────────────────────────────────

class Tool:
    """Abstract base for all PromptCraft tools.

    Each tool declares: what it's called, when it applies, how to run it,
    what safety boundary it needs, and what guidance to inject into the
    system prompt.

    Subclass and override:
      - name, description (class attrs)
      - Safety attributes: READ_ONLY, WRITES_TO_VAULT, READS_SKILLS, MODIFIES_SKILLS
      - is_applicable(request, context) → bool
      - call(request, context) → ToolResult
      - check_permissions(input, ctx) → ToolPermission
      - prompt() → str (optional, for system prompt injection)
    """

    name: str = ""
    description: str = ""

    # ── Safety attributes (Layer 2: Tool Execution Boundary) ─────────────────
    # Each tool declares its side-effect profile. The engine reads these
    # before calling check_permissions() to decide the gating strategy.

    READ_ONLY: bool = False        # Tool only reads, never writes
    WRITES_TO_VAULT: bool = False  # Tool can write to vault (checkpoint)
    READS_SKILLS: bool = False     # Tool reads Skill files from skills_dir
    MODIFIES_SKILLS: bool = False  # Tool modifies Skill files — HARD DENY for all tools

    # ── Applicability ────────────────────────────────────────────────────────

    def is_applicable(self, request: Any, context: dict[str, Any] | None = None) -> bool:
        """Can this tool handle the current request?

        Default: False (fail-closed). Tools opt-in by overriding.
        """
        return False

    def call(self, request: Any, context: dict[str, Any] | None = None) -> ToolResult:
        """Execute the tool. Override in subclasses."""
        return tool_error(f"{self.name}: not implemented")

    # ── Safety ───────────────────────────────────────────────────────────────

    def check_permissions(self, input: dict[str, Any], context: Any = None) -> Any:
        """Validate that this specific tool invocation is safe.

        Each tool implements its own checks. Cf. Claude Code:
        each tool has a checkPermissions method. This is the sub-agent
        equivalent — read tools auto-allow, write tools check vault caps,
        and MODIFIES_SKILLS is always denied.

        Returns ToolPermission(action="allow"|"deny"|"warn", reason="...").
        Default: allow (fail-open for compatibility; subclasses tighten).
        """
        from protocol import ToolPermission
        return ToolPermission(action="allow")

    # ── System prompt ────────────────────────────────────────────────────────

    def prompt(self) -> str:
        """Guidance injected into the system prompt's Layer 5.

        Return empty string if the tool needs no special instructions.
        """
        return ""

    def __repr__(self) -> str:
        return f"Tool({self.name})"
