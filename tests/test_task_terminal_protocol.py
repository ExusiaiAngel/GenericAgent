import json
import unittest
from pathlib import Path


class IncidentFixtureTests(unittest.TestCase):
    def test_captured_requests_ended_with_tool_calls(self):
        path = Path(__file__).parent / "fixtures" / "qq_incident_20260712.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for case in data.values():
            self.assertEqual(case["response_count"], 6)
            self.assertTrue(case["last_tool"])
            self.assertEqual(case["expected_terminal"], "max_turns")


class TerminalProtocolTests(unittest.TestCase):
    def test_long_task_has_larger_bounded_budget(self):
        from frontends.shared.task_protocol import turns_for_route

        self.assertEqual(turns_for_route("quick_chat"), 4)
        self.assertEqual(turns_for_route("quick_tool"), 6)
        self.assertEqual(turns_for_route("long_task"), 24)

    def test_max_turns_is_not_completed(self):
        from frontends.shared.task_protocol import terminal_from_runner

        terminal = terminal_from_runner(
            {"result": "MAX_TURNS_EXCEEDED"},
            "尝试抓取国内游戏站点。",
            max_turns=6,
        )
        self.assertEqual(terminal["state"], "max_turns")
        self.assertEqual(terminal["text"], "")
        self.assertIn("6", terminal["error"])

    def test_ask_user_becomes_needs_input(self):
        from frontends.shared.task_protocol import terminal_from_runner

        terminal = terminal_from_runner(
            {
                "result": "EXITED",
                "data": {
                    "status": "INTERRUPT",
                    "intent": "HUMAN_INTERVENTION",
                    "data": {
                        "question": "是否允许修改 memory/?",
                        "candidates": ["允许", "拒绝"],
                    },
                },
            },
            "等待确认",
            max_turns=24,
        )
        self.assertEqual(terminal["state"], "needs_input")
        self.assertEqual(terminal["question"], "是否允许修改 memory/?")

    def test_normal_completion_is_completed(self):
        from frontends.shared.task_protocol import terminal_from_runner

        terminal = terminal_from_runner(
            {"result": "CURRENT_TASK_DONE", "data": None},
            "这是最终答案",
            max_turns=4,
        )
        self.assertEqual(terminal["state"], "completed")
        self.assertEqual(terminal["text"], "这是最终答案")

