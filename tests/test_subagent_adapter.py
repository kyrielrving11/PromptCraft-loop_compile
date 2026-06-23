"""Tests for subagent_adapter.py — mode routing, parsing, formatting.

Run:  python tests/test_subagent_adapter.py
"""

import json
import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "loop-compiler"
sys.path.insert(0, str(AGENT_DIR))

from protocol import Mode, PromptCraftRequest, AgentLoopResult, AgentStatus, PromptCraftResponse
from subagent_adapter import (
    _parse_request, _build_agent_response,
    handle,
)


class TestParseRequest(unittest.TestCase):
    """_parse_request handles dict and string input."""

    def test_parse_dict(self):
        r = _parse_request({"task": "test", "mode": "build"})
        self.assertIsInstance(r, PromptCraftRequest)
        self.assertEqual(r.task, "test")
        self.assertEqual(r.mode, Mode.BUILD)  # "build" → Mode.BUILD in MODE_MAP

    def test_parse_default_mode(self):
        r = _parse_request({"task": "test"})
        self.assertEqual(r.mode, Mode.BUILD)

    def test_parse_invalid_mode_falls_back(self):
        r = _parse_request({"task": "test", "mode": "garbage"})
        self.assertEqual(r.mode, Mode.BUILD)


class TestBuildAgentResponse(unittest.TestCase):
    """_build_agent_response produces correct SubagentOutput JSON."""

    def test_response_format_structure(self):
        result = AgentLoopResult(
            status=AgentStatus.OK,
            response=PromptCraftResponse(
                status=AgentStatus.OK,
                prompt="test prompt",
            ),
        )
        health_line = "[PC: 5 records, normal]"
        raw = _build_agent_response(result, health_line, "build")
        data = json.loads(raw)
        self.assertIn("health", data)
        self.assertIn("status", data)
        self.assertIn("result", data)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["result"]["mode"], "build")
        self.assertEqual(data["result"]["prompt_or_overlay"], "test prompt")

    def test_response_error(self):
        result = AgentLoopResult(
            status=AgentStatus.ERROR,
            response=PromptCraftResponse(
                status=AgentStatus.ERROR,
                error="Something went wrong",
            ),
        )
        health_line = "[PC: 0 records, normal]"
        raw = _build_agent_response(result, health_line, "build")
        data = json.loads(raw)
        self.assertEqual(data["status"], "error")

    def test_response_stalled(self):
        # v3.4: stalled status returns error with empty prompt
        result = AgentLoopResult(
            status=AgentStatus.ERROR,
            response=PromptCraftResponse(
                status=AgentStatus.ERROR,
                error="Circuit breaker triggered — loop stalled.",
            ),
        )
        health_line = "[PC: 3 records, STALLED]"
        raw = _build_agent_response(result, health_line, "")
        data = json.loads(raw)
        self.assertEqual(data["status"], "error")


class TestHandleE2E(unittest.TestCase):
    """End-to-end handle() tests."""

    def test_handle_health_always_returned(self):
        """Every response includes a health line."""
        for mode in ["build", "analyze"]:
            raw = handle({"task": "test", "mode": mode})
            data = json.loads(raw)
            self.assertIn("health", data)
            self.assertTrue(data["health"].startswith("[PC:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
