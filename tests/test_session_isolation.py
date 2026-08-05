import threading
import unittest
from types import SimpleNamespace

from agentmain import GenericAgent


class SessionIsolationTests(unittest.TestCase):
    def test_ipc_tool_stdout_is_suppressed_from_service_journal(self):
        from agentmain import _suppress_tool_stdout

        self.assertTrue(_suppress_tool_stdout("ipc", False))
        self.assertTrue(_suppress_tool_stdout("user", True))
        self.assertFalse(_suppress_tool_stdout("user", False))

    def test_stream_transport_returns_only_unsent_suffix(self):
        from agentmain import _stream_delta

        self.assertEqual(_stream_delta("alpha beta", 6), "beta")
        self.assertEqual(_stream_delta("alpha beta", 10), "")

    def setUp(self):
        self.agent = GenericAgent.__new__(GenericAgent)
        self.agent.lock = threading.RLock()
        self.agent.llmclient = SimpleNamespace(
            backend=SimpleNamespace(history=[]),
            _pending_tool_ids=[],
            last_tools="cached",
        )
        self.agent.session_histories = {}
        self.agent.session_backend_histories = {}
        self.agent.session_generations = {}
        self.agent.active_chat_id = None
        self.agent.active_task = None
        self.agent.history = []
        self.agent.handler = None
        self.agent.is_running = False
        self.agent.stop_sig = False
        self.agent._discard_active_task = None
        self.agent._active_task_snapshot = None
        self.agent.task_queue = SimpleNamespace(qsize=lambda: 0)

    def test_chat_histories_do_not_cross(self):
        self.agent._activate_chat_session("chat-a")
        self.agent.llmclient.backend.history.append({"role": "user", "content": "secret-a"})
        self.agent._save_active_chat_session()

        self.agent._activate_chat_session("chat-b")
        self.assertEqual(self.agent.llmclient.backend.history, [])
        self.agent.llmclient.backend.history.append({"role": "user", "content": "only-b"})
        self.agent._save_active_chat_session()

        self.agent._activate_chat_session("chat-a")
        self.assertIn("secret-a", str(self.agent.llmclient.backend.history))
        self.assertNotIn("only-b", str(self.agent.llmclient.backend.history))

    def test_reset_clears_only_target_chat_and_increments_generation(self):
        self.agent.session_backend_histories = {"chat-a": ["a"], "chat-b": ["b"]}
        self.agent.session_histories = {"chat-a": ["a"], "chat-b": ["b"]}
        self.agent.session_generations = {"chat-a": 1, "chat-b": 4}

        generation = self.agent.reset_chat_session("chat-a")

        self.assertNotIn("chat-a", self.agent.session_backend_histories)
        self.assertNotIn("chat-a", self.agent.session_histories)
        self.assertIn("chat-b", self.agent.session_backend_histories)
        self.assertEqual(generation, 2)

    def test_stale_task_cannot_restore_reset_history(self):
        self.agent._activate_chat_session("chat-a", generation=0)
        self.agent.llmclient.backend.history.append({"content": "stale"})
        self.agent.reset_chat_session("chat-a")
        self.agent._save_active_chat_session("chat-a", generation=0)
        self.assertNotIn("chat-a", self.agent.session_backend_histories)

    def test_abort_is_owned_by_the_active_chat(self):
        self.agent.is_running = True
        self.agent.active_task = {"chat_id": "chat-b", "generation": 2}

        self.assertFalse(self.agent.abort_chat_task("chat-a"))
        self.assertFalse(self.agent.stop_sig)
        self.assertTrue(self.agent.abort_chat_task("chat-b"))
        self.assertTrue(self.agent.stop_sig)
        self.assertEqual(self.agent._discard_active_task, ("chat-b", 2))

    def test_aborted_turn_restores_pre_turn_history_snapshot(self):
        self.agent.session_histories["chat-a"] = ["prior user turn"]
        self.agent.session_backend_histories["chat-a"] = [
            {"role": "user", "content": "prior backend turn"}
        ]
        self.agent._activate_chat_session("chat-a", generation=0)
        snapshot = self.agent._capture_chat_snapshot("chat-a", 0)
        self.agent.history.append("partial aborted output")
        self.agent.llmclient.backend.history.append(
            {"role": "assistant", "content": "partial aborted output"}
        )

        self.assertTrue(self.agent._restore_chat_snapshot(snapshot))
        self.assertEqual(self.agent.history, ["prior user turn"])
        self.assertNotIn("partial aborted output", str(self.agent.llmclient.backend.history))
        self.assertEqual(self.agent.session_histories["chat-a"], ["prior user turn"])
