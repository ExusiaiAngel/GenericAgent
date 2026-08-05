import json
import tempfile
import unittest
from pathlib import Path

from frontends.shared import sub_agent


class RecentSubagentStatusTests(unittest.TestCase):
    def test_list_recent_subagents_reads_done_status_and_push_meta(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            d = base / "sub_100_1"
            d.mkdir()
            (d / "status.json").write_text(json.dumps({
                "id": "sub_100_1",
                "task": "write report",
                "status": "done",
                "progress": "任务完成",
                "result": "report done",
                "turns": 4,
                "created_at": "2026-07-05T20:00:00",
            }, ensure_ascii=False), encoding="utf-8")
            (d / ".ipc_meta.json").write_text(json.dumps({
                "chat_id": "511925693",
                "is_group": True,
                "pushed": False,
                "push_attempts": 2,
                "push_last_error": "send_msg timeout",
                "next_retry_at": 2000,
            }, ensure_ascii=False), encoding="utf-8")

            rows = sub_agent.list_recent_subagents(base_dir=str(base), limit=5, now=2100)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "sub_100_1")
            self.assertEqual(rows[0]["status"], "done")
            self.assertFalse(rows[0]["pushed"])
            self.assertEqual(rows[0]["push_attempts"], 2)
            self.assertEqual(rows[0]["push_last_error"], "send_msg timeout")


if __name__ == "__main__":
    unittest.main()
