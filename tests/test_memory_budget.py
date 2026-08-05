import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from memory_policy import plan_l4_retention, validate_memory_content


class MemoryBudgetTests(unittest.TestCase):
    def test_l1_rejects_more_than_thirty_lines(self):
        with self.assertRaisesRegex(ValueError, "line budget"):
            validate_memory_content("global_mem_insight.txt", "\n".join(str(i) for i in range(31)))

    def test_l2_rejects_over_byte_budget(self):
        with mock.patch.dict(os.environ, {"GENERICAGENT_MEMORY_L2_MAX_BYTES": "16"}):
            with self.assertRaisesRegex(ValueError, "budget exceeded"):
                validate_memory_content("global_mem.txt", "x" * 17)

    def test_automatic_growth_is_bounded(self):
        with mock.patch.dict(os.environ, {"GENERICAGENT_MEMORY_SETTLEMENT_MAX_GROWTH": "4"}):
            with self.assertRaisesRegex(ValueError, "growth exceeded"):
                validate_memory_content(
                    "fact.md", "123456", previous_content="", automatic=True
                )

    def test_retention_planning_is_dry_and_oldest_first(self):
        with tempfile.TemporaryDirectory() as root:
            old = Path(root, "old.txt"); old.write_text("old")
            new = Path(root, "new.txt"); new.write_text("newer")
            now = time.time()
            os.utime(old, (now - 100 * 86400, now - 100 * 86400))
            selected = plan_l4_retention(
                [old, new], now=now, max_age_days=90, max_total_bytes=100
            )
            self.assertEqual(selected, [old])
            self.assertTrue(old.exists())

    def test_rejected_patch_leaves_original_file_unchanged(self):
        from ga import file_patch
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "global_mem.txt")
            target.write_text("old", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "GENERICAGENT_MEMORY_ROOT": root,
                "GENERICAGENT_MEMORY_L2_MAX_BYTES": "4",
            }):
                result = file_patch(str(target), "old", "content too large")
            self.assertEqual(result["status"], "error")
            self.assertEqual(target.read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
