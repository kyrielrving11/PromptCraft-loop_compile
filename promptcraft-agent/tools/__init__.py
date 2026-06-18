"""PromptCraft Agent — Tool Registry.

The registry holds all available tools and matches incoming requests to
the right tool. Tools are checked in registration order — earlier tools
have higher priority.

Usage:
    from tools import registry
    tool = registry.match(request, context)
    if tool:
        result = tool.call(request, context)
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolResult, tool_error


class ToolRegistry:
    """Ordered collection of tools with priority-based matching.

    Tools registered first are checked first. The first applicable tool wins.
    """

    def __init__(self) -> None:
        self._tools: list[Tool] = []

    def register(self, tool: Tool) -> None:
        """Add a tool. Earlier registrations have higher match priority."""
        if tool.name in {t.name for t in self._tools}:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools.append(tool)

    def match(self, request: Any, context: dict[str, Any] | None = None) -> Tool | None:
        """Find the first applicable tool. Returns None if no tool matches."""
        for tool in self._tools:
            if tool.is_applicable(request, context):
                return tool
        return None

    def get(self, name: str) -> Tool | None:
        """Look up a tool by name."""
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    def call(self, request: Any, context: dict[str, Any] | None = None) -> ToolResult:
        """Match and execute in one call. The common case.

        If no tool matches, returns an error result.
        """
        tool = self.match(request, context)
        if tool is None:
            return tool_error("No applicable tool found for this request.")
        return tool.call(request, context)

    def list_prompts(self) -> str:
        """Build the system prompt injection text for all tools."""
        lines: list[str] = []
        for tool in self._tools:
            p = tool.prompt()
            if p:
                lines.append(p)
        return "\n\n".join(lines)

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools)


# ── Module-level singleton ──────────────────────────────────────────────────────

registry = ToolRegistry()
