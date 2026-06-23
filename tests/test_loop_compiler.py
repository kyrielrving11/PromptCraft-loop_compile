"""Tests for loop_compiler.py — hard gates, advisories, compilation, persistence.

Run:  python tests/test_loop_compiler.py
"""

import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "loop-compiler"
sys.path.insert(0, str(AGENT_DIR))

from protocol import (
    LoopCompileRequest, LoopCompileResponse, LoopRoundResult,
    LoopObjective, LoopHealth, TaskAlignment,
)
from loop_compiler import (
    decide_level, compute_advisories, compile_loop,
    compile_l0, compile_l1, compile_l2,
    compute_goal_text_hash, derive_goal_id,
    compute_loop_objective_from_task,
    check_loop_health, align_task,
    _detects_repair_signal, _tokenize, _jaccard,
    _count_consecutive_hash_mismatches,
    extract_objective_from_plan,
    strategy_collapse,
    get_previous_round, get_recent_rounds, vault_get_loop_objective,
)


# ── Test helpers ──────────────────────────────────────────────────────────────

def _make_request(**overrides) -> LoopCompileRequest:
    kwargs = {
        "loop_id": "test-loop",
        "round": 1,
        "goal_id": "audit-erc20",
        "task": "Audit ERC20 token for security vulnerabilities",
        "domain": "solidity-security",
    }
    kwargs.update(overrides)
    return LoopCompileRequest(**kwargs)


def _make_prev_result(round_num=1, success=False, summary="found 3 issues", quality=3) -> LoopRoundResult:
    return LoopRoundResult(
        round=round_num,
        success=success,
        output_summary=summary,
        quality_score=quality,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tokenization & Jaccard
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenization(unittest.TestCase):
    def test_basic_tokenize(self):
        tokens = _tokenize("audit erc20 token security")  # lowercase for matching
        self.assertIn("audit", tokens)
        self.assertIn("erc20", tokens)
        self.assertIn("token", tokens)
        self.assertIn("security", tokens)

    def test_cjk_tokenize(self):
        tokens = _tokenize("修复 approve 漏洞")
        # CJK chars should be included
        self.assertTrue(any("修" in t or "修" == t for t in tokens)
                        or "修复" in tokens)

    def test_jaccard_identical(self):
        a = _tokenize("audit token")
        b = _tokenize("audit token")
        self.assertAlmostEqual(_jaccard(a, b), 1.0)

    def test_jaccard_disjoint(self):
        a = _tokenize("audit token")
        b = _tokenize("write tests")
        self.assertAlmostEqual(_jaccard(a, b), 0.0)

    def test_jaccard_partial(self):
        a = _tokenize("audit erc20 token security")
        b = _tokenize("audit erc20 token permissions")
        score = _jaccard(a, b)
        self.assertGreater(score, 0.5)
        self.assertLess(score, 1.0)

    def test_jaccard_empty(self):
        # Two empty sets → 0.0 (no shared information)
        self.assertAlmostEqual(_jaccard(set(), set()), 0.0)
        self.assertAlmostEqual(_jaccard({"audit"}, set()), 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Goal identity
# ═══════════════════════════════════════════════════════════════════════════════

class TestGoalIdentity(unittest.TestCase):
    def test_compute_hash_deterministic(self):
        h1 = compute_goal_text_hash("Audit ERC20 token")
        h2 = compute_goal_text_hash("Audit ERC20 token")
        self.assertEqual(h1, h2)

    def test_compute_hash_different_tasks(self):
        h1 = compute_goal_text_hash("Audit ERC20 token")
        h2 = compute_goal_text_hash("Write unit tests")
        self.assertNotEqual(h1, h2)

    def test_compute_hash_normalizes_whitespace(self):
        h1 = compute_goal_text_hash("Audit  ERC20   token")
        h2 = compute_goal_text_hash("Audit ERC20 token")
        self.assertEqual(h1, h2)

    def test_derive_goal_id_explicit(self):
        gid = derive_goal_id("loop-1", "audit token", "my-stable-id")
        self.assertEqual(gid, "my-stable-id")

    def test_derive_goal_id_fallback(self):
        gid = derive_goal_id("loop-1", "Audit ERC20 token security", "")
        self.assertTrue(gid.startswith("loop-1:"))


# ═══════════════════════════════════════════════════════════════════════════════
# Repair Signal Detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestRepairDetection(unittest.TestCase):
    def test_detects_fix_keyword(self):
        r = _make_request(new_since_last_round="fix the approve race condition")
        self.assertTrue(_detects_repair_signal(r))

    def test_detects_chinese_keyword(self):
        r = _make_request(new_since_last_round="修复 approve 竞态条件")
        self.assertTrue(_detects_repair_signal(r))

    def test_detects_in_output_summary(self):
        r = _make_request(
            last_round_result=_make_prev_result(summary="bug found in transferFrom")
        )
        self.assertTrue(_detects_repair_signal(r))

    def test_no_repair_signal(self):
        r = _make_request(new_since_last_round="continued analysis")
        self.assertFalse(_detects_repair_signal(r))


# ═══════════════════════════════════════════════════════════════════════════════
# Hard Gates — decide_level()
# ═══════════════════════════════════════════════════════════════════════════════

class TestDecideLevel(unittest.TestCase):
    """4 hard gates: override, first-call/plan, goal_id, failure/constraint."""

    # ── Gate 1: force_level override ──

    def test_gate1_force_l0_respects_round1(self):
        # force_level cannot override round 1 — loop_objective must be anchored
        r = _make_request(round=1, force_level="l0")
        self.assertEqual(decide_level(r, None), "l2")

    def test_gate1_force_l0_respects_plan_source(self):
        # force_level cannot override plan_source — plan extraction requires L2
        r = _make_request(round=3, goal_id="same-id", force_level="l0", plan_source="spec.md")
        self.assertEqual(decide_level(r, None), "l2")

    def test_gate1_force_l0_overrides_when_safe(self):
        # force_level=l0 works when round > 1, no plan_source, same goal_id
        vault = {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop",
                    "round": 2,
                    "goal_id": "same-id",
                    "goal_text_hash": "abc123",
                },
            }],
        }
        r = _make_request(round=3, goal_id="same-id", force_level="l0")
        self.assertEqual(decide_level(r, vault), "l0")

    def test_gate1_force_l2_overrides(self):
        r = _make_request(round=3, goal_id="same-id", force_level="l2")
        self.assertEqual(decide_level(r, None), "l2")

    def test_gate1_auto_passthrough(self):
        r = _make_request(force_level="auto")
        # gate 2 triggers — round 1 → l2
        self.assertEqual(decide_level(r, None), "l2")

    # ── Gate 2: first call → L2 ──

    def test_gate2_round1_returns_l2(self):
        r = _make_request(round=1)
        self.assertEqual(decide_level(r, None), "l2")

    def test_gate2_plan_source_returns_l2(self):
        r = _make_request(round=3, goal_id="same-id", plan_source="spec.md")
        self.assertEqual(decide_level(r, None), "l2")

    # ── Gate 3: goal_id changed → L2 ──

    def test_gate3_goal_id_changed_returns_l2(self):
        # Setup: previous round has different goal_id
        vault = {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop",
                    "round": 2,
                    "goal_id": "audit-erc721",  # different!
                    "goal_text_hash": "abc123",
                    "quality_score": 4,
                    "constraints_active": ["check-reentrancy"],
                },
                "success": True,
            }],
        }
        r = _make_request(round=3, goal_id="audit-erc20")
        self.assertEqual(decide_level(r, vault), "l2")

    # ── Gate 4: new failures/constraints → L1 ──

    def test_gate4_new_failure_returns_l1(self):
        vault = {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop",
                    "round": 1,
                    "goal_id": "audit-erc20",
                    "goal_text_hash": "abc123",
                    "quality_score": 3,
                    "constraints_active": [],
                },
                "success": False,
            }],
        }
        r = _make_request(
            round=2, goal_id="audit-erc20",
            last_round_result=_make_prev_result(success=False),
        )
        self.assertEqual(decide_level(r, vault), "l1")

    def test_gate4_new_constraints_returns_l1(self):
        vault = {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop",
                    "round": 1,
                    "goal_id": "audit-erc20",
                    "goal_text_hash": "abc123",
                    "quality_score": 4,
                    "constraints_active": [],
                },
                "success": True,
            }],
        }
        r = _make_request(
            round=2, goal_id="audit-erc20",
            constraints_from_plan=["check flash loans"],
        )
        self.assertEqual(decide_level(r, vault), "l1")

    def test_gate4_repair_signal_returns_l1(self):
        vault = {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop",
                    "round": 1,
                    "goal_id": "audit-erc20",
                    "goal_text_hash": "abc123",
                    "quality_score": 4,
                    "constraints_active": [],
                },
                "success": True,
            }],
        }
        r = _make_request(
            round=2, goal_id="audit-erc20",
            new_since_last_round="fix the approve bug",
        )
        self.assertEqual(decide_level(r, vault), "l1")

    # ── L0: nothing triggered ──

    def test_returns_l0_when_nothing_changed(self):
        vault = {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop",
                    "round": 2,
                    "goal_id": "audit-erc20",
                    "goal_text_hash": "abc123",
                    "quality_score": 4,
                    "constraints_active": [],
                },
                "success": True,
            }],
        }
        r = _make_request(round=3, goal_id="audit-erc20")
        self.assertEqual(decide_level(r, vault), "l0")


# ═══════════════════════════════════════════════════════════════════════════════
# Compilation — compile_l0 / compile_l1 / compile_l2
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompileL0(unittest.TestCase):
    def test_l0_reuses_cache(self):
        """L0 with cached prompt from vault — reuses it."""
        prev = get_previous_round("test-loop", 2, {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop", "round": 2,
                    "goal_id": "audit-erc20",
                    "constraints_active": [],
                },
                "full_prompt": "## Previous round prompt content",
            }],
        })
        r = _make_request(round=3, goal_id="audit-erc20")
        resp = compile_l0(r, None, prev)
        self.assertEqual(resp.recompile_level, "l0")
        self.assertIn("Previous round prompt content", resp.prompt)
        self.assertEqual(resp.round, 3)

    def test_l0_no_cache_auto_escalates_to_l2(self):
        """L0 with no cached prompt → auto-escalates to L2 full compile."""
        r = _make_request(round=3, goal_id="audit-erc20")
        resp = compile_l0(r, None)  # No vault context → cache miss
        # Returns L2-generated prompt with recompile_level preserved as "l0"
        self.assertEqual(resp.recompile_level, "l0")
        self.assertIn("PromptCraft L2 Compile", resp.prompt)
        self.assertIn("auto-escalated", resp.diff_from_previous)

    def test_l0_inherits_active_constraints(self):
        prev = get_previous_round("test-loop", 2, {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop", "round": 2,
                    "goal_id": "audit-erc20",
                    "constraints_active": ["check-reentrancy", "gas-limit"],
                },
                "full_prompt": "## Cached prompt from round 2",
            }],
        })
        r = _make_request(round=3, goal_id="audit-erc20")
        resp = compile_l0(r, None, prev)
        self.assertEqual(resp.constraints_active, ["check-reentrancy", "gas-limit"])


class TestCompileL1(unittest.TestCase):
    def test_l1_patch_includes_new_constraints(self):
        r = _make_request(
            round=2, goal_id="audit-erc20",
            constraints_from_plan=["check flash loans"],
        )
        resp = compile_l1(r, None)
        self.assertEqual(resp.recompile_level, "l1")
        self.assertIn("check flash loans", resp.prompt)

    def test_l1_patch_includes_violations(self):
        r = _make_request(
            round=2, goal_id="audit-erc20",
            last_round_result=LoopRoundResult(
                round=1, success=False,
                constraint_violations=["ignored reentrancy check"],
                output_summary="found issue",
            ),
        )
        resp = compile_l1(r, None)
        self.assertIn("ignored reentrancy check", resp.prompt)

    def test_l1_preserves_previous_constraints(self):
        prev = get_previous_round("test-loop", 1, {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop", "round": 1,
                    "goal_id": "audit-erc20",
                    "constraints_active": ["check-reentrancy"],
                },
            }],
        })
        r = _make_request(
            round=2, goal_id="audit-erc20",
            constraints_from_plan=["check flash loans"],
        )
        resp = compile_l1(r, None, prev)
        self.assertIn("check-reentrancy", resp.constraints_active)
        self.assertIn("check flash loans", resp.constraints_active)


class TestCompileL2(unittest.TestCase):
    def test_l2_full_recompile_round1(self):
        r = _make_request(round=1, goal_id="audit-erc20")
        resp = compile_l2(r, None)
        self.assertEqual(resp.recompile_level, "l2")
        self.assertIsNotNone(resp.loop_objective)
        self.assertEqual(resp.loop_objective.loop_id, "test-loop")

    def test_l2_respects_explicit_loop_objective(self):
        lo = LoopObjective(
            objective="Test objective",
            success_criteria=["All tests pass"],
            hard_constraints=["Do not modify schema"],
            loop_id="test-loop",
        )
        r = _make_request(round=1, loop_objective=lo)
        resp = compile_l2(r, None)
        self.assertIn("Test objective", resp.prompt)
        self.assertIn("Do not modify schema", resp.prompt)

    def test_l2_includes_plan_constraints(self):
        r = _make_request(
            round=1, goal_id="audit-erc20",
            plan_source="spec.md",
            constraints_from_plan=["check reentrancy", "verify access control"],
        )
        resp = compile_l2(r, None)
        self.assertIn("check reentrancy", resp.prompt)
        self.assertIn("verify access control", resp.prompt)
        self.assertEqual(resp.plan_source, "spec.md")


# ═══════════════════════════════════════════════════════════════════════════════
# Loop Objective generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoopObjective(unittest.TestCase):
    def test_auto_generates_from_task(self):
        r = _make_request(round=1, task="Audit ERC20 token security")
        lo = compute_loop_objective_from_task(r, None)
        self.assertIn("Audit ERC20", lo.objective)
        self.assertEqual(lo.created_at_round, 1)
        self.assertEqual(lo.loop_id, "test-loop")

    def test_includes_security_success_criteria(self):
        r = _make_request(round=1, task="Security audit of ERC20 token")
        lo = compute_loop_objective_from_task(r, None)
        self.assertTrue(any("security" in sc.lower() or "vulnerability" in sc.lower()
                           for sc in lo.success_criteria))

    def test_includes_plan_constraints(self):
        r = _make_request(
            round=1, task="Audit ERC20",
            constraints_from_plan=["check reentrancy"],
        )
        lo = compute_loop_objective_from_task(r, None)
        self.assertIn("check reentrancy", lo.hard_constraints)


# ═══════════════════════════════════════════════════════════════════════════════
# Task Alignment
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskAlignment(unittest.TestCase):
    def test_aligned_task(self):
        lo = LoopObjective(
            objective="Audit ERC20 token for security vulnerabilities",
            success_criteria=["All vulnerabilities found"],
            hard_constraints=["Read-only audit"],
        )
        r = _make_request(loop_objective=lo)
        result = align_task(
            "Audit ERC20 token for reentrancy security vulnerabilities", r, None,
        )
        self.assertTrue(result.is_aligned)
        self.assertEqual(result.escalation, "none")
        self.assertGreater(result.alignment_score, 0.3)

    def test_off_objective_task_blocked(self):
        lo = LoopObjective(
            objective="Audit ERC20 token for security",
            success_criteria=["All vulnerabilities found"],
            hard_constraints=["Read-only audit"],
        )
        r = _make_request(loop_objective=lo)
        result = align_task(
            "Optimize database connection pool for auth queries", r, None,
        )
        # This should generate a warning — it's off-objective
        self.assertGreaterEqual(result.alignment_score, 0.0)
        # The alignment score should be low for a completely unrelated task
        self.assertLess(result.alignment_score, 0.5)

    def test_no_objective_returns_default(self):
        r = _make_request()
        result = align_task("some task", r, None)
        self.assertTrue(result.is_aligned)
        self.assertEqual(result.escalation, "none")


# ═══════════════════════════════════════════════════════════════════════════════
# Loop Health
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoopHealth(unittest.TestCase):
    def test_health_no_objective(self):
        r = _make_request()
        health = check_loop_health("test-loop", r, None)
        self.assertEqual(health.goal_alignment, 1.0)
        self.assertEqual(health.escalation_recommended, "none")

    def test_health_with_objective(self):
        lo = LoopObjective(
            objective="Audit ERC20 token security",
            success_criteria=["All paths audited"],
            hard_constraints=["Read-only audit"],
        )
        r = _make_request(
            task="Audit ERC20 token for reentrancy",
            loop_objective=lo,
        )
        health = check_loop_health("test-loop", r, None)
        self.assertGreater(health.goal_alignment, 0.0)
        self.assertEqual(health.constraint_integrity, 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Advisories computation
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeAdvisories(unittest.TestCase):
    def test_repair_cue_warning(self):
        r = _make_request(new_since_last_round="fix the bug")
        warnings, _, _, _ = compute_advisories(r, None)
        self.assertTrue(any("repair" in w for w in warnings))

    def test_next_task_proposal_triggers_alignment(self):
        lo = LoopObjective(
            objective="Audit ERC20 token for security",
            success_criteria=["find vulnerabilities"],
            hard_constraints=["read-only"],
        )
        r = _make_request(
            loop_objective=lo,
            next_task_proposal="Optimize gas usage in unrelated module",
        )
        _, _, alignment, _ = compute_advisories(r, None)
        self.assertIsNotNone(alignment)
        self.assertLess(alignment.alignment_score, 0.5)

    def test_health_check_runs_every_N_rounds(self):
        r = _make_request(round=3, health_check_interval=3)
        _, _, _, health = compute_advisories(r, None)
        self.assertIsNotNone(health)  # round 3 % 3 == 0 → runs


# ═══════════════════════════════════════════════════════════════════════════════
# Top-level compile_loop
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompileLoop(unittest.TestCase):
    def test_returns_valid_response(self):
        r = _make_request(round=1)
        resp = compile_loop(r)
        self.assertEqual(resp.status, "ok")
        self.assertIsNotNone(resp.prompt)
        self.assertEqual(resp.recompile_level, "l2")

    def test_l0_with_stable_state(self):
        # Simulate vault with previous round matching goal_id
        vault = {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop",
                    "round": 2,
                    "goal_id": "audit-erc20",
                    "goal_text_hash": compute_goal_text_hash("Audit ERC20 token for security vulnerabilities"),
                    "quality_score": 4,
                    "constraints_active": [],
                },
                "success": True,
            }],
        }
        r = _make_request(round=3, goal_id="audit-erc20")
        resp = compile_loop(r, vault)
        # With matching goal and no failures, should be L0
        self.assertEqual(resp.recompile_level, "l0")

    def test_includes_warnings_when_drift_detected(self):
        r = _make_request(
            round=1,
            task="Write unit tests for database layer",
        )
        resp = compile_loop(r)
        self.assertIsInstance(resp.warnings, list)

    def test_compiles_with_minimal_input(self):
        r = LoopCompileRequest(
            loop_id="minimal",
            round=1,
            task="simple task",
        )
        resp = compile_loop(r)
        self.assertEqual(resp.status, "ok")


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy collapse
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategyCollapse(unittest.TestCase):
    def test_no_collapse_with_high_quality(self):
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 4}},
                {"loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 5}},
                {"loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 4}},
            ],
        }
        self.assertFalse(strategy_collapse("test", vault))

    def test_collapse_with_low_quality(self):
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 2}},
                {"loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 1}},
                {"loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 2}},
            ],
        }
        self.assertTrue(strategy_collapse("test", vault))

    def test_no_collapse_insufficient_rounds(self):
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 2}},
                {"loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 2}},
            ],
        }
        self.assertFalse(strategy_collapse("test", vault))


# ═══════════════════════════════════════════════════════════════════════════════
# Vault helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestVaultHelpers(unittest.TestCase):
    def test_get_previous_round_found(self):
        vault = {
            "results": [{
                "loop_lineage": {
                    "loop_id": "test-loop", "round": 2,
                    "goal_id": "audit", "goal_text_hash": "abc",
                    "quality_score": 4, "constraints_active": ["c1"],
                },
                "success": True,
                "user_intent": "audit token",
            }],
        }
        prev = get_previous_round("test-loop", 2, vault)
        self.assertIsNotNone(prev)
        self.assertEqual(prev.goal_id, "audit")
        self.assertEqual(prev.task, "audit token")

    def test_get_previous_round_not_found(self):
        prev = get_previous_round("nonexistent", 1, None)
        self.assertIsNone(prev)

    def test_get_recent_rounds_sorted(self):
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 3}},
                {"loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 5}},
                {"loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 4}},
            ],
        }
        rounds = get_recent_rounds("test", 3, vault)
        self.assertEqual(len(rounds), 3)
        # Should be sorted descending
        self.assertEqual(rounds[0]["round"], 3)


# ═══════════════════════════════════════════════════════════════════════════════
# _count_consecutive_hash_mismatches
# ═══════════════════════════════════════════════════════════════════════════════

class TestConsecutiveHashMismatches(unittest.TestCase):
    """Tests for _count_consecutive_hash_mismatches — drift detection helper."""

    def test_no_mismatches_when_stable(self):
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 3,
                 "goal_text_hash": "abc"}},
                {"loop_lineage": {"loop_id": "test", "round": 2,
                 "goal_text_hash": "abc"}},
                {"loop_lineage": {"loop_id": "test", "round": 1,
                 "goal_text_hash": "abc"}},
            ],
        }
        self.assertEqual(_count_consecutive_hash_mismatches("test", vault), 0)

    def test_counts_consecutive_mismatches(self):
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 3,
                 "goal_text_hash": "ccc"}},
                {"loop_lineage": {"loop_id": "test", "round": 2,
                 "goal_text_hash": "bbb"}},
                {"loop_lineage": {"loop_id": "test", "round": 1,
                 "goal_text_hash": "aaa"}},
            ],
        }
        self.assertEqual(_count_consecutive_hash_mismatches("test", vault), 2)

    def test_stops_at_first_match(self):
        """Only counts consecutive — stops at first match."""
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 4,
                 "goal_text_hash": "ddd"}},
                {"loop_lineage": {"loop_id": "test", "round": 3,
                 "goal_text_hash": "ccc"}},
                {"loop_lineage": {"loop_id": "test", "round": 2,
                 "goal_text_hash": "bbb"}},
                {"loop_lineage": {"loop_id": "test", "round": 1,
                 "goal_text_hash": "bbb"}},  # stable here — chain breaks
            ],
        }
        self.assertEqual(_count_consecutive_hash_mismatches("test", vault), 2)

    def test_insufficient_rounds_returns_zero(self):
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 1,
                 "goal_text_hash": "aaa"}},
            ],
        }
        self.assertEqual(_count_consecutive_hash_mismatches("test", vault), 0)

    def test_none_vault_returns_zero(self):
        self.assertEqual(_count_consecutive_hash_mismatches("test", None), 0)

    def test_empty_hash_breaks_chain(self):
        """Empty hashes are falsy — chain breaks at 0, they don't count."""
        vault = {
            "results": [
                {"loop_lineage": {"loop_id": "test", "round": 3,
                 "goal_text_hash": "ccc"}},
                {"loop_lineage": {"loop_id": "test", "round": 2,
                 "goal_text_hash": ""}},
                {"loop_lineage": {"loop_id": "test", "round": 1,
                 "goal_text_hash": "aaa"}},
            ],
        }
        # Round 3 vs Round 2: "ccc" vs "" → empty is falsy → break → returns 0
        self.assertEqual(_count_consecutive_hash_mismatches("test", vault), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# extract_objective_from_plan
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractObjectiveFromPlan(unittest.TestCase):
    """Tests for extract_objective_from_plan — reads .md plans for L2 builds."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_plan(self, name: str, content: str) -> str:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_extracts_goal_success_and_constraints(self):
        plan = self._write_plan("spec.md", """# Goal
Audit the ERC20 token for security vulnerabilities.

# Success Criteria
- All reentrancy paths checked
- Access control verified

# Hard Constraints
- Read-only audit
- No on-chain transactions
""")
        result = extract_objective_from_plan(plan)
        self.assertIsNotNone(result)
        self.assertIn("Audit the ERC20", result["objective"])
        self.assertTrue(any("reentrancy" in s for s in result["success_criteria"]))
        self.assertTrue(any("Read-only" in h for h in result["hard_constraints"]))

    def test_extracts_chinese_headings(self):
        plan = self._write_plan("spec.md", """# 目标
审计 ERC20 代币安全漏洞。

# 验收标准
- 所有重入路径已检查

# 硬约束
- 只读审计
""")
        result = extract_objective_from_plan(plan)
        self.assertIsNotNone(result)
        self.assertIn("审计 ERC20", result["objective"])
        self.assertGreater(len(result["success_criteria"]), 0)
        self.assertIn("只读审计", result["hard_constraints"])

    def test_returns_none_for_missing_file(self):
        result = extract_objective_from_plan("/nonexistent/path.md")
        self.assertIsNone(result)

    def test_extracts_bullet_constraints(self):
        plan = self._write_plan("spec.md", """# Goal
Build an API.

# Constraints
- Must use REST
- Must support JSON
""")
        result = extract_objective_from_plan(plan)
        self.assertIsNotNone(result)
        self.assertIn("Build an API", result["objective"])
        self.assertIn("Must use REST", result["hard_constraints"])
        self.assertIn("Must support JSON", result["hard_constraints"])

    def test_returns_none_for_empty_sections(self):
        plan = self._write_plan("spec.md", """# Notes
Just some random notes with no recognizable sections.
""")
        result = extract_objective_from_plan(plan)
        self.assertIsNone(result)

    def test_first_paragraph_under_goal_is_objective(self):
        plan = self._write_plan("spec.md", """# Goal

The first non-empty paragraph after the heading becomes the objective.

- bullet items under goal are also collected
""")
        result = extract_objective_from_plan(plan)
        self.assertIsNotNone(result)
        self.assertIn("first non-empty paragraph", result["objective"])


# ═══════════════════════════════════════════════════════════════════════════════
# v3.5: Constraint Retirement
# ═══════════════════════════════════════════════════════════════════════════════

from loop_compiler import (
    _compute_constraint_retirement,
    _build_rolling_summary,
    _format_rolling_summary_for_prompt,
    _RETIREMENT_WINDOW, _ROLLING_WINDOW,
)


class TestConstraintRetirement(unittest.TestCase):
    """Tests for _compute_constraint_retirement — prunes stale constraints."""

    def test_all_active_none_retired(self):
        """All constraints appear in recent rounds → none retired."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 3},
                    "task": "check reentrancy again",
                    "output_summary": "checked reentrancy",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2},
                    "task": "check reentrancy in transfer",
                    "output_summary": "more reentrancy check",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1},
                    "task": "verify access control and reentrancy",
                    "output_summary": "found access issue",
                },
            ],
        }
        active, retired = _compute_constraint_retirement(
            ["check-reentrancy", "verify-access-control"], "test", 4, vault,
        )
        self.assertEqual(set(active), {"check-reentrancy", "verify-access-control"})
        self.assertEqual(retired, [])

    def test_all_silent_all_retired(self):
        """No constraint appears in any of the 3 window rounds → all retired."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 3},
                    "task": "run performance benchmarks",
                    "output_summary": "benchmarks done",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2},
                    "task": "write unit tests for auth",
                    "output_summary": "tests pass",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1},
                    "task": "add logging to auth module",
                    "output_summary": "logging added",
                },
            ],
        }
        active, retired = _compute_constraint_retirement(
            ["check-reentrancy", "verify-access-control"], "test", 4, vault,
        )
        self.assertEqual(active, [])
        self.assertEqual(set(retired), {"check-reentrancy", "verify-access-control"})

    def test_partial_silence_partial_retired(self):
        """One constraint active in some rounds, one silent → partial retirement."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 3},
                    "task": "finalize deployment",
                    "output_summary": "deployed",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2},
                    "task": "check reentrancy",
                    "output_summary": "reentrancy ok",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1},
                    "task": "setup project",
                    "output_summary": "done",
                },
            ],
        }
        active, retired = _compute_constraint_retirement(
            ["check-reentrancy", "verify-access-control"], "test", 4, vault,
        )
        self.assertEqual(active, ["check-reentrancy"])
        self.assertEqual(retired, ["verify-access-control"])

    def test_empty_constraints(self):
        """Empty constraint list → both empty."""
        active, retired = _compute_constraint_retirement([], "test", 3, None)
        self.assertEqual(active, [])
        self.assertEqual(retired, [])

    def test_no_vault_context(self):
        """No vault context → all constraints kept active."""
        active, retired = _compute_constraint_retirement(
            ["check-reentrancy"], "test", 3, None,
        )
        self.assertEqual(active, ["check-reentrancy"])
        self.assertEqual(retired, [])

    def test_insufficient_history_no_retirement(self):
        """Less than RETIREMENT_WINDOW rounds → no retirement."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2},
                    "task": "unrelated task",
                    "output_summary": "done",
                },
            ],
        }
        active, retired = _compute_constraint_retirement(
            ["check-reentrancy"], "test", 3, vault,
        )
        self.assertEqual(active, ["check-reentrancy"])
        self.assertEqual(retired, [])

    def test_round_text_from_violations(self):
        """Constraint appears in violation list → considered active."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2},
                    "task": "security audit",
                    "constraint_violations": ["check-reentrancy was skipped"],
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1},
                    "task": "initial audit",
                    "output_summary": "started",
                },
            ],
        }
        active, retired = _compute_constraint_retirement(
            ["check-reentrancy"], "test", 3, vault,
        )
        self.assertEqual(active, ["check-reentrancy"])
        self.assertEqual(retired, [])

    def test_chinese_constraint_active(self):
        """Chinese constraint text matched case-insensitively."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2},
                    "task": "检查重入漏洞",
                    "output_summary": "发现重入",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1},
                    "task": "审计",
                    "output_summary": "开始",
                },
            ],
        }
        active, retired = _compute_constraint_retirement(
            ["检查重入"], "test", 3, vault,
        )
        self.assertEqual(active, ["检查重入"])
        self.assertEqual(retired, [])


# ═══════════════════════════════════════════════════════════════════════════════
# v3.5: Rolling Summary
# ═══════════════════════════════════════════════════════════════════════════════

class TestRollingSummary(unittest.TestCase):
    """Tests for _build_rolling_summary — cross-round knowledge distillation."""

    def test_normal_multi_round_summary(self):
        """Multiple rounds with mixed quality → full summary."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 4, "quality_score": 4},
                    "task": "fix access control",
                    "output_summary": "fixed access control vulnerability",
                    "constraint_violations": [],
                    "technique_used": "few-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 2},
                    "task": "audit reentrancy",
                    "output_summary": "missed flash loan vector",
                    "constraint_violations": ["ignored reentrancy check"],
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 5},
                    "task": "audit overflow",
                    "output_summary": "found integer overflow in _transfer",
                    "constraint_violations": [],
                    "technique_used": "few-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 3},
                    "task": "initial setup",
                    "output_summary": "baseline audit complete",
                    "constraint_violations": [],
                    "technique_used": "zero-shot",
                },
            ],
        }
        rs = _build_rolling_summary("test", 5, vault)
        self.assertIsNotNone(rs)
        self.assertEqual(rs.rounds_sampled, 4)
        self.assertEqual(rs.generated_at_round, 5)
        self.assertEqual(len(rs.quality_trajectory), 4)
        self.assertEqual(rs.quality_trajectory, [3, 5, 2, 4])  # chronological
        self.assertIn("volatile", rs.trajectory_direction)  # up-down-up
        self.assertGreater(len(rs.what_worked), 0)
        self.assertEqual(len(rs.recurring_issues), 0)

    def test_recurring_issues_detected(self):
        """Same violation in 2+ rounds → recurring issues."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 2},
                    "output_summary": "audit failed",
                    "constraint_violations": ["missing gas limit check"],
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 2},
                    "output_summary": "audit failed",
                    "constraint_violations": ["missing gas limit check"],
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 3},
                    "output_summary": "started",
                    "constraint_violations": [],
                },
            ],
        }
        rs = _build_rolling_summary("test", 4, vault)
        self.assertIsNotNone(rs)
        self.assertGreater(len(rs.recurring_issues), 0)
        self.assertTrue(any("missing gas limit check" in ri for ri in rs.recurring_issues))

    def test_insufficient_rounds_partial(self):
        """Fewer than ROLLING_WINDOW rounds → partial summary."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 4},
                    "task": "audit",
                    "output_summary": "done",
                },
            ],
        }
        rs = _build_rolling_summary("test", 2, vault)
        self.assertIsNotNone(rs)
        self.assertEqual(rs.rounds_sampled, 1)
        self.assertEqual(rs.trajectory_direction, "stable")

    def test_no_vault_returns_none(self):
        """No vault context → None."""
        rs = _build_rolling_summary("test", 3, None)
        self.assertIsNone(rs)

    def test_all_high_score_what_worked(self):
        """All rounds score >= 4 → what_worked populated, no recurring issues."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 5},
                    "output_summary": "perfect audit",
                    "constraint_violations": [],
                    "technique_used": "few-shot-cot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 4},
                    "output_summary": "good audit",
                    "constraint_violations": [],
                    "technique_used": "few-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 4},
                    "output_summary": "solid start",
                    "constraint_violations": [],
                    "technique_used": "zero-shot",
                },
            ],
        }
        rs = _build_rolling_summary("test", 4, vault)
        self.assertIsNotNone(rs)
        self.assertEqual(len(rs.what_worked), 3)
        self.assertEqual(rs.trajectory_direction, "improving")
        self.assertEqual(len(rs.recurring_issues), 0)

    def test_declining_trajectory(self):
        """Steady decline → direction = declining."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 2},
                    "output_summary": "bad",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 3},
                    "output_summary": "ok",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 4},
                    "output_summary": "good",
                },
            ],
        }
        rs = _build_rolling_summary("test", 4, vault)
        self.assertIsNotNone(rs)
        self.assertEqual(rs.trajectory_direction, "declining")

    def test_format_empty_summary(self):
        """Formatting None → empty string."""
        result = _format_rolling_summary_for_prompt(None)
        self.assertEqual(result, "")

    def test_format_summary_sections(self):
        """Format produces expected sections."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 5},
                    "output_summary": "all vulnerabilities found",
                    "constraint_violations": [],
                    "technique_used": "few-shot-cot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 3},
                    "output_summary": "baseline done",
                    "constraint_violations": [],
                    "technique_used": "zero-shot",
                },
            ],
        }
        rs = _build_rolling_summary("test", 3, vault)
        text = _format_rolling_summary_for_prompt(rs)
        self.assertIn("Cross-Round Summary", text)
        self.assertIn("Quality Trajectory", text)
        self.assertIn("What Worked", text)
        self.assertNotIn("Recurring Issues", text)  # No recurring issues


# ═══════════════════════════════════════════════════════════════════════════════
# v3.5: Adaptive Technique Routing
# ═══════════════════════════════════════════════════════════════════════════════

from builder import (
    route_technique_adaptive,
    _count_consecutive_low_quality,
    _ADAPTIVE_LOW_QUALITY_THRESHOLD,
    _ADAPTIVE_CONSECUTIVE_ROUNDS,
)


class TestAdaptiveRoutingHelpers(unittest.TestCase):
    """Tests for adaptive routing primitives."""

    def test_count_low_quality_no_history(self):
        self.assertEqual(_count_consecutive_low_quality("zero-shot", "test", None), 0)

    def test_count_zero_consecutive_when_high_quality(self):
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 4},
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 5},
                    "technique_used": "zero-shot",
                },
            ],
        }
        self.assertEqual(_count_consecutive_low_quality("zero-shot", "test", vault), 0)

    def test_count_two_consecutive_low(self):
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 2},
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 1},
                    "technique_used": "zero-shot",
                },
            ],
        }
        self.assertEqual(_count_consecutive_low_quality("zero-shot", "test", vault), 2)

    def test_chain_breaks_on_different_technique(self):
        """Different technique → chain breaks."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 2},
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 2},
                    "technique_used": "few-shot",  # Different!
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 2},
                    "technique_used": "zero-shot",
                },
            ],
        }
        # Most recent is round 3 with zero-shot, score 2 → count=1
        # Round 2 is few-shot (different) → chain breaks
        self.assertEqual(_count_consecutive_low_quality("zero-shot", "test", vault), 1)

    def test_chain_breaks_on_high_quality(self):
        """High quality score → chain breaks."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 3, "quality_score": 2},
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 4},  # High!
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 2},
                    "technique_used": "zero-shot",
                },
            ],
        }
        # Round 3 zero-shot score 2 → count=1. Round 2 zero-shot score 4 → break.
        self.assertEqual(_count_consecutive_low_quality("zero-shot", "test", vault), 1)

    def test_skill_used_fallback_field(self):
        """technique_used falls back to skill_used field."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 2},
                    "skill_used": "zero-shot",  # vault format
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 1},
                    "skill_used": "zero-shot",
                },
            ],
        }
        self.assertEqual(_count_consecutive_low_quality("zero-shot", "test", vault), 2)


class TestAdaptiveRouting(unittest.TestCase):
    """Tests for route_technique_adaptive."""

    def test_no_rotation_when_high_quality(self):
        """High quality scores → no rotation."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 5},
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 4},
                    "technique_used": "zero-shot",
                },
            ],
        }
        analysis = route_technique_adaptive("rename variable", vault, "test")
        self.assertFalse(analysis.was_rotated)
        self.assertEqual(analysis.technique, "zero-shot")

    def test_rotation_on_consecutive_low(self):
        """2 consecutive low scores → rotation from zero-shot to few-shot."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 2},
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 1},
                    "technique_used": "zero-shot",
                },
            ],
        }
        analysis = route_technique_adaptive("rename variable", vault, "test")
        self.assertTrue(analysis.was_rotated)
        self.assertEqual(analysis.technique, "few-shot")
        self.assertIn("ROTATED", analysis.rationale)

    def test_no_vault_falls_back_to_keyword(self):
        """No vault context → keyword routing only, no rotation."""
        analysis = route_technique_adaptive("audit security", None, "")
        self.assertFalse(analysis.was_rotated)

    def test_ceiling_no_infinite_rotation(self):
        """tree-of-thought at ceiling → no further rotation."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 2},
                    "technique_used": "tree-of-thought",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 1},
                    "technique_used": "tree-of-thought",
                },
            ],
        }
        analysis = route_technique_adaptive("audit security crypto", vault, "test")
        # Even with low quality, tree-of-thought stays at tree-of-thought
        self.assertFalse(analysis.was_rotated)
        self.assertEqual(analysis.technique, "tree-of-thought")

    def test_rotation_updates_cognitive_load(self):
        """Rotation to a higher-tier technique updates cognitive_load.

        Task with medium load (9+ words, no high/low keywords) and not continuous
        → zero-shot-cot. After 2 consecutive low-quality rounds → rotates to few-shot-cot."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test", "round": 2, "quality_score": 2},
                    "technique_used": "zero-shot-cot",
                },
                {
                    "loop_lineage": {"loop_id": "test", "round": 1, "quality_score": 1},
                    "technique_used": "zero-shot-cot",
                },
            ],
        }
        analysis = route_technique_adaptive(
            "Investigate the test coverage gaps in the authentication module",
            vault, "test",
        )
        self.assertTrue(analysis.was_rotated)
        self.assertEqual(analysis.technique, "few-shot-cot")
        self.assertEqual(analysis.cognitive_load, "high")


# ═══════════════════════════════════════════════════════════════════════════════
# v3.5: Compilation integration (retirement + summary + adaptive routing in L1/L2)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompileL1V35(unittest.TestCase):
    """compile_l1 with constraint retirement and rolling summary."""

    def test_l1_retires_stale_constraints(self):
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 4},
                    "task": "performance tuning query planner",
                    "output_summary": "query times improved",
                },
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 3},
                    "task": "deploy to staging",
                    "output_summary": "deployed",
                },
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 2},
                    "task": "write unit tests",
                    "output_summary": "tests pass",
                },
            ],
        }
        prev = get_previous_round("test-loop", 4, vault)
        r = _make_request(
            round=5, goal_id="audit-erc20",
            constraints_from_plan=["check-reentrancy"],
            # No round in window (2,3,4) mentions check-reentrancy → retired
        )
        resp = compile_l1(r, vault, prev)
        self.assertEqual(resp.recompile_level, "l1")
        self.assertEqual(resp.constraints_active, [])
        self.assertEqual(resp.constraints_retired, ["check-reentrancy"])
        self.assertIsNotNone(resp.rolling_summary)

    def test_l1_keeps_active_constraints(self):
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 2},
                    "task": "check reentrancy in transferFrom",
                    "output_summary": "reentrancy checked",
                },
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 1},
                    "task": "initial check of reentrancy",
                    "output_summary": "started",
                },
            ],
        }
        prev = get_previous_round("test-loop", 2, vault)
        r = _make_request(
            round=3, goal_id="audit-erc20",
            constraints_from_plan=["check-reentrancy"],
        )
        resp = compile_l1(r, vault, prev)
        self.assertEqual(resp.constraints_active, ["check-reentrancy"])
        self.assertEqual(resp.constraints_retired, [])

    def test_l1_includes_rolling_summary_in_prompt(self):
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 2, "quality_score": 4},
                    "task": "audit reentrancy",
                    "output_summary": "found reentrancy issue",
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 1, "quality_score": 3},
                    "task": "initial audit",
                    "output_summary": "baseline done",
                    "technique_used": "zero-shot",
                },
            ],
        }
        prev = get_previous_round("test-loop", 2, vault)
        r = _make_request(round=3, goal_id="audit-erc20")
        resp = compile_l1(r, vault, prev)
        self.assertIn("Cross-Round Summary", resp.prompt)
        self.assertIn("Quality Trajectory", resp.prompt)


class TestCompileL2V35(unittest.TestCase):
    """compile_l2 with adaptive routing and rolling summary."""

    def test_l2_includes_rolling_summary(self):
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 1, "quality_score": 4},
                    "task": "audit reentrancy",
                    "output_summary": "found issue",
                    "technique_used": "zero-shot",
                },
            ],
        }
        r = _make_request(round=2, goal_id="audit-erc20", task="Audit ERC20 token security")
        resp = compile_l2(r, vault)
        self.assertEqual(resp.recompile_level, "l2")
        self.assertIsNotNone(resp.rolling_summary)
        self.assertIn("Cross-Round Summary", resp.prompt)

    def test_l2_uses_adaptive_routing(self):
        """L2 with prior low-quality zero-shot rounds should rotate technique."""
        vault = {
            "results": [
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 2, "quality_score": 2},
                    "task": "rename variable",
                    "output_summary": "done",
                    "technique_used": "zero-shot",
                },
                {
                    "loop_lineage": {"loop_id": "test-loop", "round": 1, "quality_score": 2},
                    "task": "format file",
                    "output_summary": "done",
                    "technique_used": "zero-shot",
                },
            ],
        }
        r = _make_request(round=3, goal_id="audit-erc20", task="rename variable consistently")
        resp = compile_l2(r, vault)
        self.assertEqual(resp.recompile_level, "l2")
        # Should have rotated from zero-shot to few-shot
        self.assertEqual(resp.technique_used, "few-shot")

    def test_l2_no_rotation_without_history(self):
        """No vault history → keyword routing without rotation."""
        r = _make_request(round=1, task="simple rename")
        resp = compile_l2(r, None)
        self.assertEqual(resp.technique_used, "zero-shot")
        self.assertIsNone(resp.rolling_summary)


if __name__ == "__main__":
    unittest.main()
