import os
import unittest

os.environ["GENERICAGENT_SKIP_SCHEDULER_LOCK"] = "1"
from reflect import scheduler


class MemorySchedulerTests(unittest.TestCase):
    def test_l4_scan_interval_is_not_a_minute(self):
        self.assertGreaterEqual(scheduler._L4_INTERVAL, 600)

    def test_l4_noop_with_only_recent_skips_is_not_actionable(self):
        self.assertFalse(
            scheduler._l4_result_is_actionable(
                {"processed": 0, "skipped": 54, "errors": 0, "deleted_raw": 0}
            )
        )
        self.assertTrue(
            scheduler._l4_result_is_actionable(
                {"processed": 1, "skipped": 54, "errors": 0, "deleted_raw": 1}
            )
        )


if __name__ == "__main__":
    unittest.main()
