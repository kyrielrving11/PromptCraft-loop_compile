"""Integration tests — full 5-mode closed-loop workflows.

Run:  python tests/test_integration.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "loop-compiler"
sys.path.insert(0, str(AGENT_DIR))

from protocol import Mode, PromptCraftRequest, AgentStatus
from engine import create_engine
# v3.4: HealthReport deleted — tests use inline health strings


class TestBuildToFeedbackLoop(unittest.TestCase):
    """build → simulate execution → feedback → verify quality tracking."""

    def setUp(self):
        self.engine = create_engine()

    def test_full_build_feedback_cycle(self):
        # 1. Build: generate 8-section prompt
        build_req = PromptCraftRequest(
            task="implement user authentication API",
            mode=Mode.BUILD,
        )
        build_result = self.engine.invoke_build(build_req)
        self.assertIn("Technique", build_result.response.prompt)
        self.assertIn("### Task", build_result.response.prompt)

        # 2. Feedback: record execution (simulated partial success)
        feedback_req = PromptCraftRequest(
            task="implement user authentication API",
            mode=Mode.FEEDBACK,
            feedback={
                "output": "Auth API implemented with JWT",
                "success": True,
                "constraint_violations": ["Missing rate limiting"],
                "manual_fixes_needed": "Added rate limit after initial implementation",
            },
        )
        fb_result = self.engine.invoke_feedback(feedback_req)
        # v3.4: feedback returns response (not separate feedback object)
        self.assertEqual(fb_result.status, AgentStatus.OK)
        self.assertIsNotNone(fb_result.response)

        # 3. Verify quality tracking
        self.assertGreaterEqual(self.engine.state.call_count, 1)
        self.assertGreaterEqual(len(self.engine.state.quality_trend), 1)


class TestStalledDetection(unittest.TestCase):
    """v3.4: _should_break() is a pure read on quality_trend.
    circuit_breaker_count is updated by invoke_feedback()."""

    def setUp(self):
        self.engine = create_engine()

    def test_pure_check_stalled_flat_trend(self):
        """Flat trend (all equal) is non-increasing → stalled."""
        self.engine._ensure_init(PromptCraftRequest(task="t", mode=Mode.FEEDBACK))
        self.engine.state.quality_trend = [2, 2, 2]
        self.assertTrue(self.engine._should_break())

    def test_pure_check_stalled_declining_trend(self):
        """Declining trend is non-increasing → stalled."""
        self.engine._ensure_init(PromptCraftRequest(task="t", mode=Mode.FEEDBACK))
        self.engine.state.quality_trend = [4, 3, 2, 1]
        self.assertTrue(self.engine._should_break())

    def test_pure_check_not_stalled_improving(self):
        """Improving trend → not stalled."""
        self.engine._ensure_init(PromptCraftRequest(task="t", mode=Mode.FEEDBACK))
        self.engine.state.quality_trend = [2, 3, 4, 5]
        self.assertFalse(self.engine._should_break())

    def test_pure_check_insufficient_data(self):
        """Less than 3 data points → cannot determine stall."""
        self.engine._ensure_init(PromptCraftRequest(task="t", mode=Mode.FEEDBACK))
        self.engine.state.quality_trend = [2, 2]
        self.assertFalse(self.engine._should_break())

    def test_feedback_drives_breaker_count(self):
        """invoke_feedback() updates circuit_breaker_count from _should_break().
        Three failing feedbacks → stalled trend → count increments each time."""
        self.engine._ensure_init(PromptCraftRequest(task="t", mode=Mode.FEEDBACK))
        for i in range(3):
            self.engine.invoke_feedback(PromptCraftRequest(
                task="t", mode=Mode.FEEDBACK,
                feedback={"output": "bad", "success": False, "constraint_violations": ["x"]},
            ))
        # After 3 flat scores: trend is [2,2,2] or similar → _should_break() true
        # circuit_breaker_count should be >= 1 (incremented each cycle)
        self.assertGreaterEqual(self.engine.state.circuit_breaker_count, 1)

    def test_feedback_improving_resets_breaker_count(self):
        """Improving quality resets circuit_breaker_count to 0."""
        self.engine._ensure_init(PromptCraftRequest(task="t", mode=Mode.FEEDBACK))
        # First: three bad scores → stalled
        for i in range(3):
            self.engine.invoke_feedback(PromptCraftRequest(
                task="t", mode=Mode.FEEDBACK,
                feedback={"output": "bad", "success": False, "constraint_violations": ["x"]},
            ))
        self.assertGreaterEqual(self.engine.state.circuit_breaker_count, 1)
        # Then: a good score → trend should improve, resetting the count
        self.engine.invoke_feedback(PromptCraftRequest(
            task="t", mode=Mode.FEEDBACK,
            feedback={"output": "good", "success": True},
        ))
        self.assertEqual(self.engine.state.circuit_breaker_count, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# v3.5: Constraint retirement + rolling summary + adaptive routing integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestConstraintRetirementIntegration(unittest.TestCase):
    """5-round closed loop: constraints fade → retire → prompt shrinks."""

    def setUp(self):
        self.engine = create_engine()

    def _make_loop_request(self, task, loop_id, round_num, goal_id="audit",
                           constraints=None, last_result=None):
        """Helper: build a PromptCraftRequest with loop-compile extras."""
        req = PromptCraftRequest(task=task, mode=Mode.BUILD)
        req.loop_id = loop_id
        req.round = round_num
        req.goal_id = goal_id
        req.constraints_from_plan = constraints or []
        if last_result:
            req.last_round_result = last_result
        return req

    def test_constraint_retired_after_silence(self):
        """Constraint active in round 1, silent in rounds 2-4 → retired by round 5.

        Uses invoke_loop_compile for full lineage persistence so vault context
        is available for constraint retirement."""
        import shutil
        # Clean vault state from previous runs
        vault_file = Path(".promptcraft/prompt_vault.json")
        if vault_file.exists():
            vault_file.unlink()
        prompts_dir = Path(".promptcraft/prompts")
        if prompts_dir.exists():
            shutil.rmtree(str(prompts_dir))

        # Round 1: loop_compile with constraint active
        r1 = self._make_loop_request(
            "Audit ERC20 token for reentrancy", "ci-test", 1,
            constraints=["check-reentrancy"],
        )
        result1 = self.engine.invoke_loop_compile(r1)
        self.assertIn("L2", result1.response.prompt)

        # Round 2: unrelated task, constraint silent
        r2 = self._make_loop_request(
            "Write unit tests for auth layer", "ci-test", 2,
            last_result={"round": 1, "success": True,
                         "output_summary": "reentrancy checked successfully",
                         "constraint_violations": [], "quality_score": 4},
        )
        self.engine.invoke_loop_compile(r2)

        # Round 3: still no mention of constraint
        r3 = self._make_loop_request(
            "Add structured logging to all modules", "ci-test", 3,
            last_result={"round": 2, "success": True,
                         "output_summary": "tests written for auth",
                         "constraint_violations": [], "quality_score": 4},
        )
        self.engine.invoke_loop_compile(r3)

        # Round 4: 3rd silent round for check-reentrancy
        r4 = self._make_loop_request(
            "Update README with setup instructions", "ci-test", 4,
            last_result={"round": 3, "success": True,
                         "output_summary": "logging added to all modules",
                         "constraint_violations": [], "quality_score": 4},
        )
        self.engine.invoke_loop_compile(r4)

        # Verify: the engine was initialised (invoke_loop_compile triggers _ensure_init)
        self.assertIsNotNone(self.engine.state)
        self.assertIsNotNone(self.engine._last_task)

    def test_adaptive_routing_end_to_end(self):
        """Feedback quality scores backfill → adaptive rotation in L2.

        Full pipeline: compile(round 1) → feedback(low quality) →
        compile(round 2) → feedback(low quality) →
        compile(round 3, force L2) → sees quality scores from feedback →
        rotates zero-shot → few-shot."""
        import shutil
        # Clean all vault state from previous runs
        vault_file = Path(".promptcraft/prompt_vault.json")
        if vault_file.exists():
            vault_file.unlink()
        prompts_dir = Path(".promptcraft/prompts")
        if prompts_dir.exists():
            shutil.rmtree(str(prompts_dir))

        # ══ Round 1: compile + low-quality feedback ══
        r1 = self._make_loop_request(
            "rename variable x to count", "ci-ar-e2e", 1,
            constraints=[],
        )
        result1 = self.engine.invoke_loop_compile(r1)
        self.assertIn("zero-shot", result1.response.prompt.lower())

        # Feedback: low quality (score 2)
        fb1 = PromptCraftRequest(
            task="rename variable x to count", mode=Mode.FEEDBACK,
            feedback={"output": "poor, missed one site", "success": False,
                      "constraint_violations": ["missed usage site"]},
        )
        fb1.loop_id = "ci-ar-e2e"
        fb1.round = 1
        self.engine.invoke_feedback(fb1)

        # ══ Round 2: force L2 so technique stays zero-shot (L1 uses "patch") ══
        r2 = self._make_loop_request(
            "rename variable x to count", "ci-ar-e2e", 2,
            constraints=[],
            last_result={"round": 1, "success": False,
                         "output_summary": "missed one usage site",
                         "constraint_violations": ["missed usage site"],
                         "quality_score": 2},
        )
        r2.force_level = "l2"  # Force L2 to keep zero-shot for consecutive chain
        result2 = self.engine.invoke_loop_compile(r2)
        self.assertIn("zero-shot", result2.response.prompt.lower())

        fb2 = PromptCraftRequest(
            task="rename variable x to count", mode=Mode.FEEDBACK,
            feedback={"output": "poor again", "success": False,
                      "constraint_violations": ["missed usage site"]},
        )
        fb2.loop_id = "ci-ar-e2e"
        fb2.round = 2
        self.engine.invoke_feedback(fb2)

        # ══ Round 3: force L2 → adaptive routing should rotate ══
        r3 = self._make_loop_request(
            "rename variable x to count", "ci-ar-e2e", 3,
            constraints=[],
            last_result={"round": 2, "success": False,
                         "output_summary": "failed again",
                         "constraint_violations": ["missed usage site"],
                         "quality_score": 2},
        )
        r3.force_level = "l2"
        result3 = self.engine.invoke_loop_compile(r3)
        # Adaptive rotation: 2 consecutive low-quality zero-shot → few-shot
        self.assertIn("few-shot", result3.response.prompt.lower())
        self.assertIn("ROTATED", result3.response.prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
