"""PromptCraft Agent — Personalization Tool.

When the main agent has a matching Skill, this tool provides a
domain-filtered overlay — only constraints relevant to that Skill's
domain are injected, not the full GLOBAL baseline.

Cf. design principle: "Skill first, PromptCraft overlay."
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolResult, tool_ok


class PersonalizationTool(Tool):
    """Filter vault constraints to produce a Skill-specific overlay.

    Triggered when request.skill_name is set (the main agent found a
    matching Skill and wants PromptCraft to personalise it).
    """

    name = "personalization"
    description = "Filter vault constraints into a domain-specific overlay for a Skill."

    # Safety: reads vault + skills, writes nothing, never modifies skills
    READ_ONLY = True
    READS_SKILLS = True

    def check_permissions(self, input: dict[str, Any], context: Any = None) -> Any:
        from protocol import tool_permission_allow, tool_permission_deny
        skill_name = input.get("skill_name", "")
        if not skill_name:
            return tool_permission_deny("Personalization requires skill_name.")
        # Skill files are read-only for this tool — allowed
        return tool_permission_allow()

    def is_applicable(self, request: Any, context: dict[str, Any] | None = None) -> bool:
        return bool(getattr(request, "skill_name", None))

    def call(self, request: Any, context: Any = None) -> ToolResult:
        skill_name = request.skill_name
        hydrate_results = (context.hydrate_results or {}) if context else {}

        global_entries = hydrate_results.get("global_entries", [])
        past_results = hydrate_results.get("results", [])

        # ── Extract constraints tagged as relevant ──
        overlay_constraints: list[str] = []
        for entry in global_entries:
            tags = entry.get("tags", [])
            if self._tags_match(skill_name, tags):
                for c in entry.get("hard_constraints_added", []):
                    if c not in overlay_constraints:
                        overlay_constraints.append(c)

        # ── Extract user/project/team preferences ──
        preferences: dict[str, str] = {}
        for entry in global_entries:
            prefs = entry.get("preferences", {})
            if prefs:
                preferences.update(prefs)

        return tool_ok(
            skill_name=skill_name,
            constraints=overlay_constraints,
            preferences=preferences,
        )

    # Domain synonym map — expands tag matching beyond literal substring overlap.
    # Cf. Claude Code's semantic recall: "deploy" should find "CI/CD".
    _SYNONYMS: dict[str, list[str]] = {
        "solidity":   ["smart-contract", "evm", "contract", "foundry", "hardhat"],
        "audit":      ["security", "review", "check", "vulnerability", "crypto"],
        "testing":    ["test", "unit-test", "integration-test", "coverage", "fuzz"],
        "api":        ["rest", "graphql", "endpoint", "route", "http"],
        "database":   ["sql", "nosql", "storage", "migration", "schema", "index"],
        "rust":       ["cargo", "wasm", "systems-programming"],
        "python":     ["django", "flask", "fastapi", "pytest"],
        "typescript": ["javascript", "node", "react", "next", "frontend"],
        "devops":     ["docker", "kubernetes", "ci-cd", "deploy", "infra"],
        "cli":        ["terminal", "shell", "bash", "command-line"],
    }

    def _tags_match(self, skill_name: str, tags: list[str]) -> bool:
        """Match tags against skill_name using direct overlap + synonym expansion.

        Example: skill_name="solidity-audit" matches tags ["solidity", "audit"]
        (direct overlap). Also matches ["smart-contract", "security"] (synonyms).
        """
        if not tags:
            return False
        name_lower = skill_name.lower().replace("-", " ").replace("_", " ")
        name_words = set(name_lower.split())

        for tag in tags:
            tag_lower = tag.lower()
            # Direct match: tag or its word appears in skill name
            if tag_lower in name_lower or tag_lower in name_words:
                return True

            # Forward: tag is a key → check if any of its synonyms match
            forward_synonyms = self._SYNONYMS.get(tag_lower, [])
            if any(s in name_lower for s in forward_synonyms):
                return True

            # Reverse: tag IS a synonym of some key → check if the key matches
            for key, syns in self._SYNONYMS.items():
                if tag_lower in syns and (key in name_lower or key in name_words):
                    return True

        return False

    def prompt(self) -> str:
        return (
            "- **Personalization**: When a Skill is matched, call this tool "
            "with skill_name to receive domain-filtered vault constraints as overlay. "
            "Use the overlay to augment — not replace — the Skill's own instructions."
        )
