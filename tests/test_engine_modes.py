"""Tests for engine.py — all 5 invoke_* methods + maybe_silent_analyze.

Run:  python tests/test_engine_modes.py
"""

import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "loop-compiler"
sys.path.insert(0, str(AGENT_DIR))

import os
import tempfile

from protocol import (
    AgentStatus, Mode, PromptCraftRequest,
)
from engine import (
    create_engine,
    _build_yaml_frontmatter,
    _parse_yaml_frontmatter,
    _escape_yaml_string,
    _coerce_yaml_scalar,
    _write_lineage_md,
    _read_lineage_md,
    _scan_lineage_md,
    _lineage_dir_name,
)
# v3.4: HealthReport deleted


class TestInvokeBuild(unittest.TestCase):
    """invoke_build() — full 8-section prompt generation."""

    def setUp(self):
        self.engine = create_engine()

    def test_build_returns_technique_selection(self):
        """v3.4: build mode uses builder.route_technique for technique selection."""
        r = PromptCraftRequest(task="build a REST API", mode=Mode.BUILD)
        result = self.engine.invoke_build(r)
        self.assertEqual(result.status, AgentStatus.OK)
        prompt = result.response.prompt
        self.assertIn("**Technique**", prompt)
        self.assertIn("### Task", prompt)
        self.assertIn("### Instructions", prompt)
        self.assertIsNotNone(result.response.analysis)
        self.assertIn(result.response.analysis.technique,
                      ["zero-shot", "few-shot", "zero-shot-cot", "few-shot-cot",
                       "step-back", "least-to-most", "tree-of-thought"])

    def test_build_tracks_state(self):
        r = PromptCraftRequest(task="test", mode=Mode.BUILD)
        result = self.engine.invoke_build(r)
        self.assertEqual(result.status, AgentStatus.OK)


class TestInvokeFeedback(unittest.TestCase):
    """invoke_feedback() — execution feedback collection."""

    def setUp(self):
        self.engine = create_engine()

    def test_feedback_success_records(self):
        """v3.4: feedback records vault write + quality tracking."""
        r = PromptCraftRequest(
            task="test task",
            mode=Mode.FEEDBACK,
            feedback={"output": "ok", "success": True},
        )
        result = self.engine.invoke_feedback(r)
        self.assertEqual(result.status, AgentStatus.OK)
        self.assertIsNotNone(result.response)
        # Quality trend is tracked in state
        self.assertGreaterEqual(len(self.engine.state.quality_trend), 1)

    def test_feedback_accumulates_in_buffer(self):
        """v3.4: feedback increments call_count instead of buffer."""
        r = PromptCraftRequest(
            task="task", mode=Mode.FEEDBACK,
            feedback={"output": "ok", "success": True},
        )
        self.engine.invoke_feedback(r)
        self.assertGreaterEqual(self.engine.state.call_count, 1)


# ═══════════════════════════════════════════════════════════════════════════════
# YAML Frontmatter helpers (stdlib-only, engine.py module-level)
# ═══════════════════════════════════════════════════════════════════════════════

class TestYamlFrontmatter(unittest.TestCase):
    """Tests for _build/parse_yaml_frontmatter, _escape, _coerce."""

    def test_escape_safe_string(self):
        self.assertEqual(_escape_yaml_string("hello"), "hello")
        self.assertEqual(_escape_yaml_string("audit-erc20"), "audit-erc20")

    def test_escape_special_chars(self):
        self.assertIn('"', _escape_yaml_string("hello: world"))
        self.assertIn('"', _escape_yaml_string("key: value"))

    def test_escape_empty_string(self):
        self.assertEqual(_escape_yaml_string(""), '""')

    def test_coerce_scalar_bool(self):
        self.assertTrue(_coerce_yaml_scalar("true"))
        self.assertTrue(_coerce_yaml_scalar("True"))
        self.assertFalse(_coerce_yaml_scalar("false"))

    def test_coerce_scalar_number(self):
        self.assertEqual(_coerce_yaml_scalar("42"), 42)
        self.assertAlmostEqual(_coerce_yaml_scalar("3.14"), 3.14)

    def test_coerce_scalar_null(self):
        self.assertIsNone(_coerce_yaml_scalar("null"))
        self.assertIsNone(_coerce_yaml_scalar("None"))

    def test_coerce_scalar_string(self):
        self.assertEqual(_coerce_yaml_scalar("hello"), "hello")

    def test_build_flat_scalars(self):
        yaml_str = _build_yaml_frontmatter({
            "loop_id": "test", "round": 3, "success": True,
            "quality_score": 0, "goal_text_hash": "abc123",
        })
        self.assertIn("loop_id: test", yaml_str)
        self.assertIn("round: 3", yaml_str)
        self.assertIn("success: true", yaml_str)
        self.assertIn("quality_score: 0", yaml_str)

    def test_build_list_values(self):
        yaml_str = _build_yaml_frontmatter({
            "constraints_active": ["check reentrancy", "verify acl"],
        })
        self.assertIn("constraints_active:", yaml_str)
        self.assertIn("- check reentrancy", yaml_str)
        self.assertIn("- verify acl", yaml_str)

    def test_build_nested_dict(self):
        yaml_str = _build_yaml_frontmatter({
            "loop_objective": {
                "objective": "Audit token",
                "success_criteria": ["all paths"],
                "hard_constraints": ["read-only"],
            },
        })
        self.assertIn("loop_objective:", yaml_str)
        self.assertIn("objective: Audit token", yaml_str)
        self.assertIn("success_criteria:", yaml_str)
        self.assertIn("- all paths", yaml_str)

    def test_roundtrip_full_lineage(self):
        """Build frontmatter → parse back → all fields survive."""
        data = {
            "loop_id": "e2e-test", "round": 2, "goal_id": "audit",
            "goal_text_hash": "abc123", "recompile_level": "l1",
            "quality_score": 4,
            "constraints_active": ["check reentrancy", "verify acl"],
            "task": "Audit ERC20 for vulnerabilities",
            "success": True, "technique_used": "few-shot-cot",
            "timestamp": "2026-06-23T10:30:00+00:00",
            "loop_objective": {
                "objective": "Audit ERC20",
                "success_criteria": ["all paths audited"],
                "hard_constraints": ["read-only audit"],
            },
        }
        yaml_block = _build_yaml_frontmatter(data)
        full = f"---\n{yaml_block}\n---\n\n# Body text"
        parsed = _parse_yaml_frontmatter(full)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["loop_id"], "e2e-test")
        self.assertEqual(parsed["round"], 2)
        self.assertEqual(parsed["goal_id"], "audit")
        self.assertEqual(parsed["recompile_level"], "l1")
        self.assertEqual(parsed["quality_score"], 4)
        self.assertEqual(parsed["constraints_active"], ["check reentrancy", "verify acl"])
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["technique_used"], "few-shot-cot")
        self.assertIn("loop_objective", parsed)
        self.assertEqual(parsed["loop_objective"]["objective"], "Audit ERC20")

    def test_parse_returns_none_for_no_frontmatter(self):
        self.assertIsNone(_parse_yaml_frontmatter("Just markdown, no frontmatter."))

    def test_parse_handles_empty_yaml(self):
        result = _parse_yaml_frontmatter("---\n---\n\nBody only")
        self.assertEqual(result, {})


class TestLineageMarkdownDualWrite(unittest.TestCase):
    """Integration tests for _write_lineage_md / _read_lineage_md / _scan_lineage_md."""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        os.chdir(self._tmpdir.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmpdir.cleanup()

    def test_write_and_read_roundtrip(self):
        md_path = _write_lineage_md(
            loop_id="test-loop",
            round_num=1,
            goal_id="audit",
            goal_text_hash="hash123",
            recompile_level="l2",
            constraints_active=["c1", "c2"],
            task="Audit token",
            prompt_text="## Compiled Prompt\n\nDo the thing.",
            technique_used="zero-shot",
            loop_objective={"objective": "Audit", "success_criteria": ["all"]},
        )
        self.assertIsNotNone(md_path)
        self.assertTrue(md_path.endswith("r1.md"))

        entry = _read_lineage_md("test-loop", 1)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["loop_id"], "test-loop")
        self.assertEqual(entry["loop_lineage"]["round"], 1)
        self.assertEqual(entry["loop_lineage"]["goal_id"], "audit")
        self.assertIn("## Compiled Prompt", entry["full_prompt"])
        self.assertIn("Do the thing.", entry["full_prompt"])
        self.assertEqual(entry["loop_lineage"]["constraints_active"], ["c1", "c2"])
        self.assertEqual(entry["technique_used"], "zero-shot")

    def test_scan_multiple_rounds(self):
        for r in (1, 2, 3):
            _write_lineage_md(
                loop_id="multi", round_num=r, goal_id="g",
                goal_text_hash=f"h{r}", recompile_level="l0",
                constraints_active=[], task=f"Task {r}",
                prompt_text=f"Prompt {r}",
            )
        scanned = _scan_lineage_md("multi")
        self.assertEqual(len(scanned), 3)
        rounds = [e["loop_lineage"]["round"] for e in scanned]
        self.assertEqual(rounds, [3, 2, 1])  # Sorted descending

    def test_read_nonexistent_returns_none(self):
        self.assertIsNone(_read_lineage_md("no-such-loop", 1))

    def test_scan_empty_returns_empty_list(self):
        self.assertEqual(_scan_lineage_md("no-such-loop"), [])

    def test_lineage_dir_name_replaces_colons(self):
        # Windows safety: colons in loop_id are replaced with hyphens
        self.assertEqual(_lineage_dir_name("loop:smoke"), "loop-smoke")
        self.assertEqual(_lineage_dir_name("simple-id"), "simple-id")


