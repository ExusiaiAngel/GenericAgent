import os
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import agentmain


class MemorySettlementTriggerTests(unittest.TestCase):
    def _handler(self, turn=3):
        return SimpleNamespace(current_turn=turn)

    def test_completed_long_task_runs_settlement(self):
        task = {"chat_route": "long_task", "source": "ipc"}
        terminal = {"state": "completed"}
        with patch.dict(os.environ, {"GENERICAGENT_AUTO_MEMORY": "1"}):
            self.assertTrue(agentmain._should_settle_memory(task, terminal, self._handler()))

    def test_quick_chat_and_noncompleted_terminals_do_not_settle(self):
        with patch.dict(os.environ, {"GENERICAGENT_AUTO_MEMORY": "1"}):
            self.assertFalse(agentmain._should_settle_memory(
                {"chat_route": "quick_chat"}, {"state": "completed"}, self._handler()
            ))
            for state in ("failed", "needs_input", "max_turns", "stopped"):
                self.assertFalse(agentmain._should_settle_memory(
                    {"chat_route": "long_task"}, {"state": state}, self._handler()
                ))

    def test_settlement_can_be_disabled(self):
        with patch.dict(os.environ, {"GENERICAGENT_AUTO_MEMORY": "0"}):
            self.assertFalse(agentmain._should_settle_memory(
                {"chat_route": "long_task"}, {"state": "completed"}, self._handler()
            ))


class IpcTerminalVisibilityTests(unittest.TestCase):
    def test_completed_terminal_is_cleaned_before_ipc(self):
        terminal = agentmain._normalize_ipc_terminal(
            {"source": "ipc"},
            {
                "state": "completed",
                "text": "**LLM Running (Turn 1) ...**\n\n最终结论：成功。\n\n`````\n[Info] Final response to user.\n`````",
                "error": "",
            },
        )
        self.assertEqual(terminal["state"], "completed")
        self.assertEqual(terminal["text"], "最终结论：成功。")

    def test_tool_only_terminal_is_not_completed(self):
        terminal = agentmain._normalize_ipc_terminal(
            {"source": "ipc"},
            {
                "state": "completed",
                "text": "**LLM Running (Turn 2) ...**\n\n`````\n[Action] Writing file\n[Info] Final response to user.\n`````",
                "error": "",
            },
        )
        self.assertEqual(terminal["state"], "failed")
        self.assertEqual(terminal["text"], "")
        self.assertIn("最终答案", terminal["error"])


class HiddenSettlementTests(unittest.TestCase):
    def setUp(self):
        self.agent = object.__new__(agentmain.GenericAgent)
        backend = SimpleNamespace(history=[{"role": "assistant", "content": "user answer"}])
        self.agent.llmclient = SimpleNamespace(backend=backend)
        self.agent.verbose = True
        self.agent.task_dir = None
        self.agent.active_task = {"chat_id": "test-chat"}

    def test_hidden_settlement_restores_backend_history(self):
        before = deepcopy(self.agent.llmclient.backend.history)

        def fake_loop(client, *args, **kwargs):
            client.backend.history.append({"role": "assistant", "content": "hidden settlement"})
            if False:
                yield None
            return {"result": "CURRENT_TASK_DONE"}

        with (
            patch("agentmain.agent_runner_loop", fake_loop),
            patch("agentmain.get_system_prompt", return_value="system"),
        ):
            result = self.agent._run_memory_settlement(
                {"query": "research", "chat_route": "long_task"},
                SimpleNamespace(history_info=[]),
            )

        self.assertEqual(result["result"], "CURRENT_TASK_DONE")
        self.assertEqual(self.agent.llmclient.backend.history, before)

    def test_hidden_settlement_restores_history_on_exception(self):
        before = deepcopy(self.agent.llmclient.backend.history)

        def failing_loop(client, *args, **kwargs):
            client.backend.history.append({"role": "assistant", "content": "leak"})
            raise RuntimeError("settlement failed")
            yield

        with (
            patch("agentmain.agent_runner_loop", failing_loop),
            patch("agentmain.get_system_prompt", return_value="system"),
        ):
            with self.assertRaises(RuntimeError):
                self.agent._run_memory_settlement(
                    {"query": "research", "chat_route": "long_task"},
                    SimpleNamespace(history_info=[]),
                )

        self.assertEqual(self.agent.llmclient.backend.history, before)

    def test_missing_final_answer_repair_is_tool_free_and_visible(self):
        captured = {}

        def fake_loop(client, system_prompt, user_input, handler, tools, **kwargs):
            captured["tools"] = tools
            yield "修复后的最终答案。\n"
            return {"result": "CURRENT_TASK_DONE"}

        with (
            patch("agentmain.agent_runner_loop", fake_loop),
            patch("agentmain.get_system_prompt", return_value="system"),
        ):
            text = self.agent._repair_missing_final_answer(
                {"query": "task"}, SimpleNamespace()
            )

        self.assertEqual(captured["tools"], [])
        self.assertIn("修复后的最终答案", text)


if __name__ == "__main__":
    unittest.main()
