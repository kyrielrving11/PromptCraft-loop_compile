"""Tests for health_report.py — threshold gating, stall detection, consistency.

Run:  python tests/test_health_report.py
"""

import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "promptcraft-agent"
sys.path.insert(0, str(AGENT_DIR))

from health_report import (
    HealthReport, compute_health, check_health,
    _compute_consistency,
    ANALYSIS_THRESHOLD, EVOLUTION_THRESHOLD, CREATION_THRESHOLD,
    CONSISTENCY_THRESHOLD, STALLED_THRESHOLD,
)


def _make_records(n: int, quality: int = 4, overlays: list[str] | None = None):
    """Helper: create n synthetic feedback records."""
    return [
        {"quality_score": quality, "overlay_used": overlays or []}
        for _ in range(n)
    ]


class TestHealthReportCompute(unittest.TestCase):
    """Threshold boundary tests for HealthReport.compute()."""

    def test_empty_buffer(self):
        """0 records → normal, no action."""
        h = HealthReport.compute([])
        self.assertEqual(h.feedback_buffer_size, 0)
        self.assertFalse(h.pattern_detected)
        self.assertFalse(h.evolution_ready)
        self.assertFalse(h.creation_ready)
        self.assertFalse(h.stalled)
        self.assertEqual(h.recommended_action, "none")
        self.assertIn("Normal", h.summary)

    def test_below_analysis_threshold(self):
        """9 records → still normal (threshold is 10)."""
        h = HealthReport.compute(_make_records(9))
        self.assertEqual(h.feedback_buffer_size, 9)
        self.assertFalse(h.pattern_detected)
        self.assertEqual(h.recommended_action, "none")

    def test_at_analysis_threshold(self):
        """10 records → pattern_detected, action=run_analysis."""
        h = HealthReport.compute(_make_records(10))
        self.assertEqual(h.feedback_buffer_size, 10)
        self.assertTrue(h.pattern_detected)
        self.assertFalse(h.evolution_ready)
        self.assertFalse(h.creation_ready)
        self.assertEqual(h.recommended_action, "run_analysis")
        self.assertIn("Pattern detected", h.summary)

    def test_below_evolution_threshold(self):
        """19 records → pattern but no evolution yet."""
        h = HealthReport.compute(_make_records(19))
        self.assertTrue(h.pattern_detected)
        self.assertFalse(h.evolution_ready)
        self.assertEqual(h.recommended_action, "run_analysis")

    def test_evolution_with_high_consistency(self):
        """20 records + same overlays → evolution_ready."""
        records = _make_records(20, overlays=["check-gas", "check-reentrancy"])
        h = HealthReport.compute(records)
        self.assertTrue(h.evolution_ready)
        self.assertFalse(h.creation_ready)
        self.assertEqual(h.recommended_action, "review_evolution")
        self.assertIn("High-consistency", h.summary)

    def test_evolution_with_low_consistency(self):
        """20 records + different overlays → no evolution."""
        records = _make_records(20, overlays=["a"])
        for i, r in enumerate(records):
            r["overlay_used"] = [f"item-{i}"]  # All different
        h = HealthReport.compute(records)
        self.assertFalse(h.evolution_ready)
        self.assertEqual(h.recommended_action, "run_analysis")

    def test_creation_threshold(self):
        """30 records → creation_ready."""
        records = _make_records(30, overlays=["check-gas"])
        h = HealthReport.compute(records)
        self.assertTrue(h.creation_ready)
        self.assertEqual(h.recommended_action, "review_creation")
        self.assertIn("Strong pattern", h.summary)

    def test_stalled_detection_flat(self):
        """3 consecutive same low scores at the END → stalled."""
        records = _make_records(7) + [
            {"quality_score": 2, "overlay_used": []},
            {"quality_score": 2, "overlay_used": []},
            {"quality_score": 2, "overlay_used": []},
        ]  # 10 total, last 3 are flat 2s
        h = HealthReport.compute(records)
        self.assertTrue(h.stalled)
        self.assertEqual(h.recommended_action, "stalled_needs_human")
        self.assertIn("Circuit breaker", h.summary)

    def test_stalled_detection_declining(self):
        """3 declining scores at the END → stalled."""
        records = _make_records(7) + [
            {"quality_score": 4, "overlay_used": []},
            {"quality_score": 3, "overlay_used": []},
            {"quality_score": 2, "overlay_used": []},
        ]
        h = HealthReport.compute(records)
        self.assertTrue(h.stalled)

    def test_not_stalled_when_improving(self):
        """Improving scores → not stalled."""
        records = _make_records(7) + [
            {"quality_score": 2, "overlay_used": []},
            {"quality_score": 3, "overlay_used": []},
            {"quality_score": 4, "overlay_used": []},
        ]
        h = HealthReport.compute(records)
        self.assertFalse(h.stalled)

    def test_analysis_ran_flag(self):
        """analysis_ran_this_time is passed through correctly."""
        h = HealthReport.compute(_make_records(10), analysis_ran=True)
        self.assertTrue(h.analysis_ran_this_time)
        h2 = HealthReport.compute(_make_records(10), analysis_ran=False)
        self.assertFalse(h2.analysis_ran_this_time)


class TestConsistency(unittest.TestCase):
    """Tests for _compute_consistency()."""

    def test_consistency_empty(self):
        self.assertEqual(_compute_consistency([]), 0.0)

    def test_consistency_single_record(self):
        self.assertEqual(_compute_consistency([{"overlay_used": ["a"]}]), 0.0)

    def test_consistency_identical(self):
        """All records have same overlays → 1.0."""
        records = [{"overlay_used": ["a", "b"]} for _ in range(5)]
        self.assertAlmostEqual(_compute_consistency(records), 1.0)

    def test_consistency_disjoint(self):
        """No overlap → 0.0."""
        records = [
            {"overlay_used": ["a"]},
            {"overlay_used": ["b"]},
        ]
        self.assertAlmostEqual(_compute_consistency(records), 0.0)

    def test_consistency_partial(self):
        """50% overlap → 0.5."""
        records = [
            {"overlay_used": ["a", "b"]},
            {"overlay_used": ["b", "c"]},
        ]
        # a,b vs b,c: intersection={b}=1, union={a,b,c}=3, jaccard=1/3≈0.33
        self.assertAlmostEqual(_compute_consistency(records), 1 / 3)


class TestCompactStr(unittest.TestCase):
    """Tests for compact_str() / compact_line()."""

    def test_compact_normal(self):
        h = HealthReport(feedback_buffer_size=15)
        self.assertIn("15 records", h.compact_str())
        self.assertIn("normal", h.compact_str())

    def test_compact_action(self):
        h = HealthReport(
            feedback_buffer_size=25,
            evolution_ready=True,
            recommended_action="review_evolution",
        )
        s = h.compact_str()
        self.assertIn("25 records", s)
        self.assertIn("review_evolution", s)

    def test_compact_stalled(self):
        h = HealthReport(
            feedback_buffer_size=8,
            stalled=True,
            recommended_action="stalled_needs_human",
        )
        s = h.compact_str()
        self.assertIn("STALLED", s)
        self.assertIn("stalled_needs_human", s)

    def test_compact_line_alias(self):
        """compact_line() is alias for compact_str()."""
        h = HealthReport.compute(_make_records(5))
        self.assertEqual(h.compact_line(), h.compact_str())


class TestCheckHealth(unittest.TestCase):
    """Tests for standalone check_health()."""

    def test_check_health_empty(self):
        h = check_health()
        self.assertEqual(h.feedback_buffer_size, 0)
        self.assertEqual(h.recommended_action, "none")

    def test_check_health_with_aggregate(self):
        agg = {
            "results": [
                {
                    "group_key": "solidity_audit",
                    "total_records": 15,
                    "avg_quality": 3.5,
                    "high_freq_overlays": [
                        {"overlay": "check-gas", "pct": 80},
                    ],
                }
            ]
        }
        h = check_health(vault_aggregate=agg)
        self.assertEqual(h.feedback_buffer_size, 15)
        self.assertTrue(h.pattern_detected)
        self.assertEqual(h.recommended_action, "run_analysis")

    def test_check_health_with_buffer_padding(self):
        """When buffer_size exceeds aggregate, pads with empty records."""
        h = check_health(buffer_size=5)
        self.assertEqual(h.feedback_buffer_size, 5)


class TestBackwardCompat(unittest.TestCase):
    """Tests for the backward-compatible compute_health() wrapper."""

    def test_compute_health_empty(self):
        h = compute_health(buffer_size=0, quality_trend=[])
        self.assertEqual(h.feedback_buffer_size, 0)

    def test_compute_health_with_trend(self):
        h = compute_health(buffer_size=15, quality_trend=[3, 4, 4, 5])
        self.assertEqual(h.feedback_buffer_size, 15)
        self.assertTrue(h.pattern_detected)

    def test_compute_health_stalled(self):
        h = compute_health(buffer_size=10, quality_trend=[2, 2, 2])
        self.assertTrue(h.stalled)


class TestProactiveSignals(unittest.TestCase):
    """Phase 5: proactive signals in HealthReport."""

    def test_proactive_signals_empty_by_default(self):
        """New HealthReport has empty list."""
        h = HealthReport()
        self.assertEqual(h.proactive_signals, [])

    def test_proactive_signals_in_compute(self):
        """HealthReport.compute passes proactive_signals through."""
        h = HealthReport.compute(_make_records(5, 4), proactive_signals=["3 vault entries match"])
        self.assertEqual(h.proactive_signals, ["3 vault entries match"])

    def test_compact_str_with_proactive_signals(self):
        """compact_str includes signal count when signals present."""
        h = HealthReport.compute(
            _make_records(10, 4),
            proactive_signals=["s1", "s2"],
        )
        line = h.compact_str()
        self.assertIn("signals=2", line)

    def test_compact_str_without_signals(self):
        """compact_str unchanged when no signals."""
        h = HealthReport.compute(_make_records(10, 4))
        line = h.compact_str()
        self.assertNotIn("signals", line)

    def test_compute_passes_empty_list_through(self):
        """None proactive_signals becomes empty list."""
        h = HealthReport.compute(_make_records(5, 4))
        self.assertEqual(h.proactive_signals, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
