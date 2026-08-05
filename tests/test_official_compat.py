import unittest
from types import SimpleNamespace

from ga import GenericAgentHandler
from llmcore import BaseSession


class OfficialBehaviorCompatibilityTests(unittest.TestCase):
    def test_turn_summary_prefers_visible_cleaned_response_over_tool_arguments(self):
        parent = SimpleNamespace(task_dir=None, _turn_end_hooks={})
        handler = GenericAgentHandler(parent, last_history=[])
        handler._in_plan_mode = lambda: None
        response = SimpleNamespace(content="已完成检查并修复问题。")

        handler.turn_end_callback(
            response,
            [{"tool_name": "code_run", "args": {"script": "secret command"}}],
            [],
            1,
            "",
            {},
        )

        self.assertIn("已完成检查并修复问题", handler.history_info[-1])
        self.assertNotIn("secret command", handler.history_info[-1])

    def test_context_window_scales_tool_limits_with_a_cap(self):
        session = BaseSession({
            "apikey": "test",
            "apibase": "https://example.invalid",
            "model": "large-context-model",
            "context_win": 200000,
        })
        parent = SimpleNamespace(get_ctx_multiplier=lambda: session.maxlen_multiplier)
        handler = GenericAgentHandler(parent, last_history=[])

        self.assertEqual(session.maxlen_multiplier, 3.0)
        self.assertEqual(session.cut_msg_interval, 15)
        self.assertEqual(handler._get_tool_maxlen(10000, {"_tool_num": 2}), 15000)
        self.assertEqual(
            handler._get_tool_maxlen(35000, {"_tool_num": 2}, growth_rate=0.5),
            35000,
        )


if __name__ == "__main__":
    unittest.main()
