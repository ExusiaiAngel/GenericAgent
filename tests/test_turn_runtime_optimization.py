import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agentmain
import agent_loop
import ga
from frontends.shared.task_protocol import turns_for_source


class TurnRuntimeOptimizationTests(unittest.TestCase):
    def test_missing_type_infers_bash_for_shell_command(self):
        script = "cd /opt/GenericAgent && ls -la memory/report_scheduler.py"
        self.assertEqual(ga._infer_code_type(script, None), "bash")

    def test_missing_type_keeps_python_as_python(self):
        self.assertEqual(
            ga._infer_code_type("import os\nprint(os.getcwd())", None),
            "python",
        )
        self.assertEqual(ga._infer_code_type("echo ok", "python"), "python")

    def test_curated_runtime_roots_expose_business_code_without_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            for name in ("memory", "reflect", "sche_tasks", "frontends"):
                (project / name).mkdir()
            with patch.object(ga, "script_dir", str(project)):
                roots = ga._configured_code_read_roots()

            self.assertIn((project / "memory").resolve(), roots)
            self.assertIn((project / "reflect").resolve(), roots)
            self.assertIn((project / "sche_tasks").resolve(), roots)
            self.assertNotIn(project.resolve(), roots)

    def test_reflect_tasks_are_isolated_and_have_a_bounded_turn_budget(self):
        agent = agentmain.GenericAgent.__new__(agentmain.GenericAgent)
        agent.task_queue = queue.Queue(maxsize=4)

        agent.put_task("scheduled work", source="reflect")
        task = agent.task_queue.get_nowait()

        self.assertTrue(task["reset_history"])
        self.assertEqual(task["max_turns"], turns_for_source("reflect"))
        self.assertLessEqual(task["max_turns"], 16)

    def test_reset_history_clears_backend_and_tool_protocol_state(self):
        backend = SimpleNamespace(history=[{"role": "user", "content": "old"}])
        client = SimpleNamespace(
            backend=backend,
            last_tools="cached tools",
            _pending_tool_ids=["old-tool"],
        )
        agent = SimpleNamespace(history=["old summary"], llmclient=client)

        agentmain._reset_model_context(agent)

        self.assertEqual(agent.history, [])
        self.assertEqual(backend.history, [])
        self.assertEqual(client.last_tools, "")
        self.assertEqual(client._pending_tool_ids, [])

    def test_reflect_does_not_reinject_a_previous_context_checkpoint(self):
        self.assertFalse(agentmain._should_inject_context_checkpoint("reflect"))
        self.assertTrue(agentmain._should_inject_context_checkpoint("user"))

    def test_source_budgets_preserve_explicit_limits(self):
        self.assertEqual(turns_for_source("reflect"), 16)
        self.assertEqual(turns_for_source("task"), 40)
        self.assertEqual(turns_for_source("user"), 40)
        self.assertEqual(turns_for_source("reflect", requested=7), 7)

    def test_stuck_warning_is_sent_back_to_the_model(self):
        class FakeClient:
            def __init__(self):
                self.messages = []
                self.last_tools = ""

            def chat(self, messages, tools=None):
                self.messages.append(messages)
                call = SimpleNamespace(
                    function=SimpleNamespace(name="file_read", arguments='{"path":"missing"}'),
                    id="tool-1",
                )
                response = SimpleNamespace(content="retry", tool_calls=[call])

                def generated():
                    if False:
                        yield None
                    return response

                return generated()

        class FakeHandler:
            def __init__(self):
                self.parent = SimpleNamespace(task_dir=None)
                self._done_hooks = []
                self.current_turn = 0

            def dispatch(self, *_args, **_kwargs):
                def generated():
                    if False:
                        yield None
                    return agent_loop.StepOutcome(
                        {"status": "error"}, next_prompt="try again"
                    )

                return generated()

            def turn_end_callback(
                self, response, tool_calls, tool_results, turn, next_prompt, exit_reason
            ):
                return next_prompt

        client = FakeClient()
        runner = agent_loop.agent_runner_loop(
            client,
            "system",
            "user",
            FakeHandler(),
            [],
            max_turns=5,
            verbose=False,
        )
        agent_loop.exhaust(runner)

        final_prompt = client.messages[-1][0]["content"]
        self.assertIn("重复工具调用", final_prompt)


if __name__ == "__main__":
    unittest.main()
