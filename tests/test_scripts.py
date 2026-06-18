"""Tests for prompt-memory scripts (checkpoint.py + hydrate.py).

Run:  python tests/test_scripts.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "prompt-memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import checkpoint
import hydrate


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_utc_now_returns_iso_string(self):
        result = checkpoint._utc_now()
        self.assertIsInstance(result, str)
        self.assertIn("T", result)

    def test_truncate_short_text(self):
        self.assertEqual(checkpoint._truncate("hello"), "hello")

    def test_truncate_long_text(self):
        long_text = "a" * 300
        result = checkpoint._truncate(long_text)
        self.assertLessEqual(len(result), checkpoint.MAX_PREVIEW_CHARS + 3)
        self.assertTrue(result.endswith("..."))

    def test_truncate_empty(self):
        self.assertEqual(checkpoint._truncate(""), "")

    def test_truncate_none(self):
        self.assertEqual(checkpoint._truncate(None), "")

    def test_read_vault_missing(self):
        vault_path = self.tmp_path / "nonexistent.json"
        data = checkpoint._read_vault(vault_path)
        self.assertEqual(data, {"version": "1", "entries": []})

    def test_read_vault_valid(self):
        vault_path = self.tmp_path / "vault.json"
        vault_path.write_text(
            '{"version": "1", "entries": [{"id": "a", "task_id": "t1"}]}',
            encoding="utf-8",
        )
        data = checkpoint._read_vault(vault_path)
        self.assertEqual(len(data["entries"]), 1)

    def test_read_vault_corrupted(self):
        vault_path = self.tmp_path / "bad.json"
        vault_path.write_text("{not valid json", encoding="utf-8")
        data = checkpoint._read_vault(vault_path)
        self.assertEqual(data, {"version": "1", "entries": []})

    def test_read_vault_filters_non_dict_entries(self):
        vault_path = self.tmp_path / "vault.json"
        vault_path.write_text(
            '{"entries": [{"id": "a"}, "bad_string", 123, {"id": "b"}]}',
            encoding="utf-8",
        )
        data = checkpoint._read_vault(vault_path)
        self.assertEqual(len(data["entries"]), 2)
        self.assertTrue(all(isinstance(e, dict) for e in data["entries"]))

    def test_list_field_from_list(self):
        result = checkpoint._list_field({"tags": ["  foo ", "bar  ", ""]}, "tags")
        self.assertEqual(result, ["foo", "bar"])

    def test_list_field_from_string(self):
        result = checkpoint._list_field({"tags": "  solo  "}, "tags")
        self.assertEqual(result, ["solo"])

    def test_list_field_empty(self):
        self.assertEqual(checkpoint._list_field({}, "tags"), [])

    def test_validate_entry_missing_required(self):
        with self.assertRaises(ValueError) as ctx:
            checkpoint._validate_entry({"task_id": "t1"})
        self.assertIn("user_intent", str(ctx.exception))

    def test_validate_entry_ok(self):
        checkpoint._validate_entry({"task_id": "t1", "user_intent": "do stuff"})

    def test_find_active_found(self):
        vault = {
            "entries": [
                {"task_id": "t1", "is_active": True, "id": "a"},
                {"task_id": "t1", "is_active": False, "id": "b"},
            ]
        }
        result = checkpoint._find_active(vault, "t1")
        self.assertEqual(result["id"], "a")

    def test_find_active_not_found(self):
        vault = {"entries": [{"task_id": "t1", "is_active": False, "id": "b"}]}
        self.assertIsNone(checkpoint._find_active(vault, "t1"))

    def test_count_versions(self):
        vault = {"entries": [
            {"task_id": "t1"}, {"task_id": "t1"}, {"task_id": "t2"},
        ]}
        self.assertEqual(checkpoint._count_versions(vault, "t1"), 2)
        self.assertEqual(checkpoint._count_versions(vault, "t2"), 1)
        self.assertEqual(checkpoint._count_versions(vault, "t3"), 0)

    def test_build_entry_new_task(self):
        vault = {"version": "1", "entries": []}
        prompts_dir = self.tmp_path / "prompts"
        payload = {
            "task_id": "my-task",
            "user_intent": "do something",
            "skill_used": "zero-shot",
            "generated_prompt": "# Full prompt content",
            "hard_constraints": ["c1", "c2"],
            "tags": ["tag1"],
        }
        entry = checkpoint._build_entry(payload, vault, prompts_dir, None)
        self.assertEqual(entry["task_id"], "my-task")
        self.assertEqual(entry["version_tag"], "v1")
        self.assertTrue(entry["is_active"])
        self.assertIsNone(entry["parent_version"])
        self.assertEqual(entry["skill_used"], "zero-shot")
        self.assertEqual(entry["hard_constraints"], ["c1", "c2"])
        self.assertIn("md_path", entry)
        md_file = self.tmp_path / entry["md_path"]
        self.assertTrue(md_file.exists())
        self.assertEqual(
            md_file.read_text(encoding="utf-8").strip(), "# Full prompt content"
        )

    def test_build_entry_version_bump(self):
        vault = {
            "version": "1",
            "entries": [{
                "id": "old", "task_id": "my-task", "version_tag": "v1",
                "is_active": True, "parent_version": None,
                "timestamp": "2026-01-01T00:00:00Z", "user_intent": "original",
            }],
        }
        prompts_dir = self.tmp_path / "prompts"
        payload = {
            "task_id": "my-task",
            "user_intent": "improved version",
            "generated_prompt": "# V2 prompt",
        }
        entry = checkpoint._build_entry(payload, vault, prompts_dir, version_of="my-task")
        self.assertEqual(entry["version_tag"], "v2")
        self.assertEqual(entry["parent_version"], "v1")
        self.assertTrue(entry["is_active"])
        old = next(e for e in vault["entries"] if e["id"] == "old")
        self.assertFalse(old["is_active"])

    def test_build_entry_version_of_mismatch(self):
        vault = {"version": "1", "entries": []}
        prompts_dir = self.tmp_path / "prompts"
        payload = {"task_id": "task-a", "user_intent": "test"}
        with self.assertRaises(ValueError) as ctx:
            checkpoint._build_entry(payload, vault, prompts_dir, version_of="task-b")
        self.assertIn("does not match", str(ctx.exception))

    def test_build_entry_no_prompt(self):
        vault = {"version": "1", "entries": []}
        prompts_dir = self.tmp_path / "prompts"
        payload = {"task_id": "dry-run", "user_intent": "just metadata"}
        entry = checkpoint._build_entry(payload, vault, prompts_dir, None)
        self.assertNotIn("md_path", entry)
        self.assertEqual(entry["generated_prompt_preview"], "")

    def test_write_vault_creates_parent_dir(self):
        vault_path = self.tmp_path / "sub" / "vault.json"
        checkpoint._write_vault(vault_path, {"version": "1", "entries": []})
        self.assertTrue(vault_path.exists())

    def test_write_prompt_md(self):
        prompts_dir = self.tmp_path / "prompts"
        checkpoint._write_prompt_md(prompts_dir, "t1", "v1", "# Hello World\n")
        expected = self.tmp_path / "prompts" / "t1" / "v1.md"
        self.assertTrue(expected.exists())
        self.assertEqual(expected.read_text(encoding="utf-8").strip(), "# Hello World")


class TestHydrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokenize_chinese(self):
        tokens = hydrate._tokenize("审计智能合约的权限控制逻辑")
        self.assertIn("审", tokens)
        self.assertIn("计", tokens)

    def test_tokenize_english(self):
        tokens = hydrate._tokenize("audit smart contract")
        self.assertIn("audit", tokens)
        self.assertIn("smart", tokens)
        self.assertIn("contract", tokens)

    def test_tokenize_mixed(self):
        tokens = hydrate._tokenize("ERC-20 合约 audit")
        self.assertIn("erc", tokens)
        self.assertIn("合", tokens)

    def test_tokenize_japanese(self):
        tokens = hydrate._tokenize("スマートコントラクトの監査")
        self.assertIn("ス", tokens)  # katakana
        self.assertIn("の", tokens)  # hiragana

    def test_tokenize_korean(self):
        tokens = hydrate._tokenize("스마트 계약 감사")
        self.assertIn("스", tokens)  # hangul

    def test_jaccard_identical(self):
        a, b = {"a", "b", "c"}, {"a", "b", "c"}
        self.assertEqual(hydrate._jaccard(a, b), 1.0)

    def test_jaccard_disjoint(self):
        a, b = {"a", "b"}, {"c", "d"}
        self.assertEqual(hydrate._jaccard(a, b), 0.0)

    def test_jaccard_empty(self):
        self.assertEqual(hydrate._jaccard(set(), {"a"}), 0.0)
        self.assertEqual(hydrate._jaccard({"a"}, set()), 0.0)

    def test_score_weights(self):
        query_tokens = hydrate._tokenize("audit contract security")
        entry = {
            "user_intent": "audit ERC-20 smart contract permissions",
            "tags": ["security", "solidity"],
            "hard_constraints": [],
            "key_decisions": [],
            "execution_feedback": "",
        }
        score = hydrate._score(query_tokens, entry)
        self.assertGreater(score, 0)

    def test_is_global_true(self):
        self.assertTrue(hydrate._is_global({"summary": {"importance": "GLOBAL"}}))

    def test_is_global_false(self):
        self.assertFalse(hydrate._is_global({"summary": {"importance": "STAGE"}}))
        self.assertFalse(hydrate._is_global({"summary": {}}))
        self.assertFalse(hydrate._is_global({}))

    def test_read_vault_error_handling(self):
        vault_path = self.tmp_path / "corrupt.json"
        vault_path.write_text("{{{bad", encoding="utf-8")
        data = hydrate._read_vault(vault_path)
        self.assertEqual(data, {"version": "1", "entries": []})

    def test_compact_entry_default(self):
        entry = {
            "id": "e1", "task_id": "t1", "version_tag": "v1",
            "is_active": True, "parent_version": None,
            "timestamp": "2026-01-01T00:00:00Z", "skill_used": "zero-shot",
            "user_intent": "test", "hard_constraints": [], "key_decisions": [],
            "execution_feedback": "", "tags": [], "score": 0.5,
            "summary": {"goal": "test goal", "technique": "zero-shot"},
        }
        compact = hydrate._compact_entry(entry)
        self.assertEqual(compact["id"], "e1")
        self.assertEqual(compact["summary"]["goal"], "test goal")
        self.assertNotIn("generated_prompt", compact)

    def test_compact_entry_include_prompt_from_md(self):
        prompts_dir = self.tmp_path / "prompts" / "t1"
        prompts_dir.mkdir(parents=True)
        md_file = prompts_dir / "v1.md"
        md_file.write_text("# Full prompt here", encoding="utf-8")

        entry = {
            "id": "e1", "task_id": "t1", "version_tag": "v1",
            "is_active": True, "parent_version": None,
            "timestamp": "2026-01-01T00:00:00Z", "skill_used": "",
            "user_intent": "", "hard_constraints": [], "key_decisions": [],
            "execution_feedback": "", "tags": [], "score": 0,
            "md_path": str(md_file.as_posix()),
            "generated_prompt_preview": "# Full...",
        }
        compact = hydrate._compact_entry(entry, include_prompt=True)
        self.assertEqual(compact["generated_prompt"], "# Full prompt here")


class TestFederation(unittest.TestCase):
    """Tests for multi-project federation (global vault merge)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_global_entries_no_global(self):
        """Without global vault, entries are unchanged and tagged project."""
        project = [
            {"id": "p1", "task_id": "t1", "is_active": True},
            {"id": "p2", "task_id": "t2", "is_active": False},
        ]
        merged = hydrate._merge_global_entries(project, None)
        self.assertEqual(len(merged), 2)
        self.assertTrue(all(e["source"] == "project" for e in merged))

    def test_merge_global_entries_new_task(self):
        """Global entry with new task_id is merged in."""
        project = [
            {"id": "p1", "task_id": "t1", "is_active": True},
        ]
        global_vault = {
            "entries": [
                {"id": "g1", "task_id": "t2", "is_active": True, "user_intent": "global"},
            ]
        }
        merged = hydrate._merge_global_entries(project, global_vault)
        self.assertEqual(len(merged), 2)
        sources = {e["id"]: e["source"] for e in merged}
        self.assertEqual(sources["p1"], "project")
        self.assertEqual(sources["g1"], "global")

    def test_merge_global_entries_dedup(self):
        """Global entry is skipped when project already has active entry for task_id."""
        project = [
            {"id": "p1", "task_id": "shared-task", "is_active": True},
        ]
        global_vault = {
            "entries": [
                {"id": "g1", "task_id": "shared-task", "is_active": True},
                {"id": "g2", "task_id": "other-task", "is_active": True},
            ]
        }
        merged = hydrate._merge_global_entries(project, global_vault)
        self.assertEqual(len(merged), 2)  # p1 + g2 (g1 deduped)
        ids = {e["id"] for e in merged}
        self.assertIn("p1", ids)
        self.assertIn("g2", ids)
        self.assertNotIn("g1", ids)

    def test_merge_global_entries_inactive_skipped(self):
        """Inactive global entries are not merged."""
        project = [{"id": "p1", "task_id": "t1", "is_active": True}]
        global_vault = {
            "entries": [
                {"id": "g1", "task_id": "t2", "is_active": False},
            ]
        }
        merged = hydrate._merge_global_entries(project, global_vault)
        self.assertEqual(len(merged), 1)

    def test_checkpoint_global_flag(self):
        """checkpoint.py --global writes to ~/.promptcraft/global_vault.json."""
        home_global = Path.home() / ".promptcraft" / "global_vault.json"
        # Ensure clean state
        if home_global.exists():
            home_global.unlink()

        try:
            import subprocess
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "checkpoint.py"),
                    "--global",
                ],
                input='{"task_id":"fed-test","user_intent":"test federation"}',
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(home_global.exists())
            data = json.loads(home_global.read_text(encoding="utf-8"))
            self.assertEqual(len(data["entries"]), 1)
            self.assertEqual(data["entries"][0]["task_id"], "fed-test")
        finally:
            if home_global.exists():
                home_global.unlink()
            prompts_dir = Path.home() / ".promptcraft" / "prompts"
            import shutil
            if prompts_dir.exists():
                shutil.rmtree(prompts_dir)


class TestAggregate(unittest.TestCase):
    """Tests for hydrate.py --aggregate mode (v3 memory module)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_aggregate_basic(self):
        """Aggregate groups entries by task_type and returns correct counts."""
        vault_path = self.tmp_path / "vault.json"
        vault = {"version": "1", "entries": []}
        prompts_dir = self.tmp_path / "prompts"

        # Create 12 solidity_audit entries with varying quality
        for i in range(12):
            payload = {
                "task_id": f"audit-{i}", "user_intent": f"audit {i}",
                "task_type": "solidity_audit",
                "quality_score": 5 if i < 8 else 2,
                "overlay_used": ["gas-check"] if i % 2 == 0 else [],
            }
            entry = checkpoint._build_entry(payload, vault, prompts_dir, None)
            # _build_entry appends to vault["entries"] and sets is_active
        checkpoint._write_vault(vault_path, vault)

        # Reread and aggregate
        vault2 = hydrate._read_vault(vault_path)

        class Args:
            group_by = "task_type"
            min_records = 10
            task_type = None

        import io, sys
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        hydrate.cmd_aggregate(Args(), vault2)
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

        data = json.loads(output)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["groups"], 1)
        self.assertEqual(data["results"][0]["group_key"], "solidity_audit")
        self.assertEqual(data["results"][0]["total_records"], 12)

    def test_aggregate_min_records(self):
        """Groups below min_records threshold are filtered out."""
        vault_path = self.tmp_path / "vault.json"
        vault = {"version": "1", "entries": []}
        prompts_dir = self.tmp_path / "prompts"

        for i in range(5):
            payload = {
                "task_id": f"api-{i}", "user_intent": f"api {i}",
                "task_type": "api_design", "quality_score": 4,
            }
            checkpoint._build_entry(payload, vault, prompts_dir, None)
        checkpoint._write_vault(vault_path, vault)

        vault2 = hydrate._read_vault(vault_path)

        class Args:
            group_by = "task_type"
            min_records = 10
            task_type = None

        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        hydrate.cmd_aggregate(Args(), vault2)
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

        data = json.loads(output)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["groups"], 0)  # 5 < 10, filtered out

    def test_aggregate_gate(self):
        """Three-tier gate markers are assigned correctly."""
        vault_path = self.tmp_path / "vault.json"
        vault = {"version": "1", "entries": []}
        prompts_dir = self.tmp_path / "prompts"

        # 10 records → pattern_ready
        for i in range(10):
            payload = {
                "task_id": f"task-{i}", "user_intent": f"task {i}",
                "task_type": "pattern_test",
                "quality_score": 4,
            }
            checkpoint._build_entry(payload, vault, prompts_dir, None)
        checkpoint._write_vault(vault_path, vault)

        vault2 = hydrate._read_vault(vault_path)

        class Args:
            group_by = "task_type"
            min_records = 10
            task_type = None

        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        hydrate.cmd_aggregate(Args(), vault2)
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

        data = json.loads(output)
        self.assertEqual(data["groups"], 1)
        self.assertEqual(data["results"][0]["gate"], "pattern_ready")


class TestFreshness(unittest.TestCase):
    """Tests for freshness calculation in hydrate.py (v3 memory module)."""

    def test_freshness_today(self):
        """Timestamp from today returns 'today' and no warning."""
        ts_today = "2026-06-17T10:00:00Z"
        self.assertEqual(hydrate._freshness(ts_today), "today")
        self.assertEqual(hydrate._freshness_warning(ts_today), "")

    def test_freshness_warning(self):
        """Timestamp from 3 days ago returns warning text."""
        ts_old = "2026-06-14T10:00:00Z"
        freshness = hydrate._freshness(ts_old)
        self.assertIn("days ago", freshness)
        warning = hydrate._freshness_warning(ts_old)
        self.assertIn("point-in-time", warning)
        self.assertIn("Verify against current code", warning)

    def test_freshness_unknown(self):
        """Empty or invalid timestamp returns 'unknown' and no warning."""
        self.assertEqual(hydrate._freshness(""), "unknown")
        self.assertEqual(hydrate._freshness_warning(""), "")
        self.assertEqual(hydrate._freshness("not-a-timestamp"), "unknown")
        self.assertEqual(hydrate._freshness_warning("not-a-timestamp"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
