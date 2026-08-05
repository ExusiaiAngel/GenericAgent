import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ga import GenericAgentHandler


def exhaust_with_output(generator):
    output = []
    while True:
        try:
            output.append(next(generator))
        except StopIteration as stopped:
            return stopped.value, "".join(output)


class WebFetchWritePolicyTests(unittest.TestCase):
    def test_save_to_file_outside_sandbox_is_denied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            sandbox = temp_root / "sandbox"
            sandbox.mkdir()
            outside_path = temp_root / "outside.txt"
            handler = GenericAgentHandler(SimpleNamespace(), cwd=str(temp_root))
            fetch_result = {
                "status": "success",
                "content": "body",
                "url": "https://example.com",
            }

            with (
                patch("ga.DEFAULT_WRITE_ROOT", str(sandbox)),
                patch.dict(os.environ, {"GENERICAGENT_WRITE_ROOTS": ""}),
                patch("ga.web_fetch", return_value=fetch_result),
            ):
                outcome, _ = exhaust_with_output(
                    handler.do_web_fetch(
                        {
                            "url": "https://example.com",
                            "save_to_file": "outside.txt",
                        },
                        SimpleNamespace(),
                    )
                )

            self.assertEqual(outcome.data["status"], "error")
            self.assertIn("Write denied", outcome.data["msg"])
            self.assertFalse(outside_path.exists())

    def test_save_to_file_inside_sandbox_writes_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            sandbox = temp_root / "sandbox"
            sandbox.mkdir()
            inside_path = sandbox / "inside.txt"
            handler = GenericAgentHandler(SimpleNamespace(), cwd=str(temp_root))
            fetch_result = {
                "status": "success",
                "content": "body",
                "url": "https://example.com",
            }

            with (
                patch("ga.DEFAULT_WRITE_ROOT", str(sandbox)),
                patch.dict(os.environ, {"GENERICAGENT_WRITE_ROOTS": ""}),
                patch("ga.web_fetch", return_value=fetch_result),
            ):
                outcome, _ = exhaust_with_output(
                    handler.do_web_fetch(
                        {
                            "url": "https://example.com",
                            "save_to_file": "sandbox/inside.txt",
                        },
                        SimpleNamespace(),
                    )
                )

            self.assertEqual(outcome.data["status"], "success")
            self.assertEqual(inside_path.read_text(encoding="utf-8"), "body")


class MemoryWritePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.sandbox = self.root / "sandbox"
        self.memory = self.root / "memory"
        self.sandbox.mkdir()
        self.memory.mkdir()
        self.handler = GenericAgentHandler(SimpleNamespace(), cwd=str(self.root))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _policy(self):
        return (
            patch("ga.DEFAULT_WRITE_ROOT", str(self.sandbox)),
            patch.dict(
                os.environ,
                {
                    "GENERICAGENT_WRITE_ROOTS": "",
                    "GENERICAGENT_MEMORY_ROOT": str(self.memory),
                },
                clear=True,
            ),
        )

    def test_file_patch_can_update_top_level_markdown_memory(self):
        target = self.memory / "example_sop.md"
        target.write_text("old", encoding="utf-8")

        policy_root, policy_env = self._policy()
        with policy_root, policy_env:
            outcome, _ = exhaust_with_output(
                self.handler.do_file_patch(
                    {
                        "path": "memory/example_sop.md",
                        "old_content": "old",
                        "new_content": "new",
                    },
                    SimpleNamespace(),
                )
            )

        self.assertEqual(outcome.data["status"], "success")
        self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_file_patch_can_update_l2_text_memory(self):
        target = self.memory / "global_mem.txt"
        target.write_text("old", encoding="utf-8")

        policy_root, policy_env = self._policy()
        with policy_root, policy_env:
            outcome, _ = exhaust_with_output(
                self.handler.do_file_patch(
                    {
                        "path": "memory/global_mem.txt",
                        "old_content": "old",
                        "new_content": "new",
                    },
                    SimpleNamespace(),
                )
            )

        self.assertEqual(outcome.data["status"], "success")
        self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_memory_patch_rejects_nested_code_and_symlink_targets(self):
        nested = self.memory / "nested"
        nested.mkdir()
        (nested / "note.md").write_text("old", encoding="utf-8")
        (self.memory / "tool.py").write_text("old", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("old", encoding="utf-8")
        symlink = self.memory / "escape.md"
        try:
            symlink.symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation is unavailable")

        policy_root, policy_env = self._policy()
        with policy_root, policy_env:
            for relative in (
                "memory/nested/note.md",
                "memory/tool.py",
                "memory/escape.md",
            ):
                with self.subTest(relative=relative):
                    outcome, _ = exhaust_with_output(
                        self.handler.do_file_patch(
                            {
                                "path": relative,
                                "old_content": "old",
                                "new_content": "new",
                            },
                            SimpleNamespace(),
                        )
                    )
                    self.assertEqual(outcome.data["status"], "error")

        self.assertEqual(outside.read_text(encoding="utf-8"), "old")

    def test_file_write_only_creates_new_top_level_markdown_memory(self):
        existing = self.memory / "existing.md"
        existing.write_text("keep", encoding="utf-8")

        policy_root, policy_env = self._policy()
        with policy_root, policy_env:
            existing_outcome, _ = exhaust_with_output(
                self.handler.do_file_write(
                    {
                        "path": "memory/existing.md",
                        "mode": "overwrite",
                        "content": "replace",
                    },
                    SimpleNamespace(content=""),
                )
            )
            new_outcome, _ = exhaust_with_output(
                self.handler.do_file_write(
                    {
                        "path": "memory/new_sop.md",
                        "mode": "overwrite",
                        "content": "new memory",
                    },
                    SimpleNamespace(content=""),
                )
            )
            code_outcome, _ = exhaust_with_output(
                self.handler.do_file_write(
                    {
                        "path": "memory/new_tool.py",
                        "mode": "overwrite",
                        "content": "print('unsafe')",
                    },
                    SimpleNamespace(content=""),
                )
            )

        self.assertEqual(existing_outcome.data["status"], "error")
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
        self.assertEqual(new_outcome.data["status"], "success")
        self.assertEqual(
            (self.memory / "new_sop.md").read_text(encoding="utf-8"),
            "new memory",
        )
        self.assertEqual(code_outcome.data["status"], "error")
        self.assertFalse((self.memory / "new_tool.py").exists())

    def test_memory_only_handler_rejects_normal_sandbox_write(self):
        target = self.sandbox / "report.md"
        handler = GenericAgentHandler(
            SimpleNamespace(), cwd=str(self.root), memory_only=True
        )
        policy_root, policy_env = self._policy()
        with policy_root, policy_env:
            outcome, _ = exhaust_with_output(
                handler.do_file_write(
                    {"path": "sandbox/report.md", "content": "blocked"},
                    SimpleNamespace(content=""),
                )
            )
        self.assertEqual(outcome.data["status"], "error")
        self.assertFalse(target.exists())

    def test_memory_patch_rejects_secret_like_content(self):
        target = self.memory / "global_mem.txt"
        target.write_text("old", encoding="utf-8")
        handler = GenericAgentHandler(
            SimpleNamespace(), cwd=str(self.root), memory_only=True
        )
        policy_root, policy_env = self._policy()
        with policy_root, policy_env:
            outcome, _ = exhaust_with_output(
                handler.do_file_patch(
                    {
                        "path": "memory/global_mem.txt",
                        "old_content": "old",
                        "new_content": "api_key = sk-secret-value-1234567890",
                    },
                    SimpleNamespace(),
                )
            )
        self.assertEqual(outcome.data["status"], "error")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_memory_write_rejects_volatile_metadata(self):
        handler = GenericAgentHandler(
            SimpleNamespace(), cwd=str(self.root), memory_only=True
        )
        policy_root, policy_env = self._policy()
        with policy_root, policy_env:
            outcome, _ = exhaust_with_output(
                handler.do_file_write(
                    {
                        "path": "memory/volatile.md",
                        "content": "# Fact\n- Ubuntu 24.04.4 LTS（验证时间：2026-07-13）",
                    },
                    SimpleNamespace(content=""),
                )
            )
        self.assertEqual(outcome.data["status"], "error")
        self.assertFalse((self.memory / "volatile.md").exists())


class ExternalCapabilityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.handler = GenericAgentHandler(SimpleNamespace(), cwd=os.getcwd())

    def test_mcp_call_is_denied_by_default_before_subprocess(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ga.subprocess.run") as run,
        ):
            outcome, output = exhaust_with_output(
                self.handler.do_mcp_call(
                    {"server": "github", "tool": "create_issue", "args": {}},
                    SimpleNamespace(),
                )
            )

        run.assert_not_called()
        self.assertIn("not allowlisted", outcome.data["error"])
        self.assertIn("not allowlisted", output)

    def test_mcp_call_allows_an_exact_allowlist_entry(self):
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        with (
            patch.dict(
                os.environ,
                {"GENERICAGENT_MCP_ALLOWLIST": "other/tool, noise/tool; github/create_issue beta/*"},
                clear=True,
            ),
            patch("ga.subprocess.run", return_value=completed) as run,
        ):
            outcome, _ = exhaust_with_output(
                self.handler.do_mcp_call(
                    {"server": "github", "tool": "create_issue", "args": {}},
                    SimpleNamespace(),
                )
            )

        run.assert_called_once()
        self.assertEqual(outcome.data["result"], "ok")

    def test_mcp_call_allows_a_server_wildcard_entry(self):
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        with (
            patch.dict(
                os.environ,
                {"GENERICAGENT_MCP_ALLOWLIST": "github/*"},
                clear=True,
            ),
            patch("ga.subprocess.run", return_value=completed) as run,
        ):
            outcome, _ = exhaust_with_output(
                self.handler.do_mcp_call(
                    {"server": "github", "tool": "list_repos", "args": {}},
                    SimpleNamespace(),
                )
            )

        run.assert_called_once()
        self.assertEqual(outcome.data["result"], "ok")

    def test_qq_group_op_is_disabled_by_default_before_subprocess(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ga.subprocess.run") as run,
        ):
            outcome, output = exhaust_with_output(
                self.handler.do_qq_group_op(
                    {"op": "notice", "group_id": "123456", "text": "hello"},
                    SimpleNamespace(),
                )
            )

        run.assert_not_called()
        self.assertIn("disabled", outcome.data["error"])
        self.assertIn("disabled", output)

    def test_qq_group_op_rejects_invalid_group_id_before_subprocess(self):
        with (
            patch.dict(
                os.environ,
                {"GENERICAGENT_QQ_ADMIN_ENABLED": "true"},
                clear=True,
            ),
            patch("ga.subprocess.run") as run,
        ):
            outcome, _ = exhaust_with_output(
                self.handler.do_qq_group_op(
                    {"op": "notice", "group_id": "12x", "text": "hello"},
                    SimpleNamespace(),
                )
            )

        run.assert_not_called()
        self.assertIn("group_id", outcome.data["error"])

    def test_qq_group_op_rejects_invalid_operation_arguments_before_subprocess(self):
        invalid_cases = (
            ({"op": "ban", "group_id": "123", "user_id": "abc", "duration": 60}, "user_id"),
            ({"op": "ban", "group_id": "123", "user_id": "456", "duration": 0}, "duration"),
            ({"op": "ban", "group_id": "123", "user_id": "456", "duration": 2592001}, "duration"),
            ({"op": "ban", "group_id": "123", "user_id": "456", "duration": "60"}, "duration"),
            ({"op": "notice", "group_id": "123", "text": "   "}, "text"),
        )
        for args, error_field in invalid_cases:
            with self.subTest(args=args):
                with (
                    patch.dict(
                        os.environ,
                        {"GENERICAGENT_QQ_ADMIN_ENABLED": "on"},
                        clear=True,
                    ),
                    patch("ga.subprocess.run") as run,
                ):
                    outcome, _ = exhaust_with_output(
                        self.handler.do_qq_group_op(args, SimpleNamespace())
                    )

                run.assert_not_called()
                self.assertIn(error_field, outcome.data["error"])

    def test_qq_group_op_kick_passes_typed_json_payload_to_subprocess(self):
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        with (
            patch.dict(
                os.environ,
                {"GENERICAGENT_QQ_ADMIN_ENABLED": "true"},
                clear=True,
            ),
            patch("ga.subprocess.run", return_value=completed) as run,
        ):
            outcome, _ = exhaust_with_output(
                self.handler.do_qq_group_op(
                    {"op": "kick", "group_id": "123456", "user_id": "789012"},
                    SimpleNamespace(),
                )
            )

        run.assert_called_once()
        argv = run.call_args.args[0]
        payload = json.loads(argv[-1])
        self.assertEqual(payload["action"], "set_group_kick")
        self.assertEqual(payload["params"]["group_id"], 123456)
        self.assertEqual(payload["params"]["user_id"], 789012)
        self.assertIs(payload["params"]["reject_add_request"], False)
        self.assertEqual(outcome.data["result"], "ok")


class ToolSchemaPolicyTests(unittest.TestCase):
    @staticmethod
    def _contract_shape(value):
        if isinstance(value, dict):
            return {
                key: ToolSchemaPolicyTests._contract_shape(item)
                for key, item in value.items()
                if key != "description"
            }
        if isinstance(value, list):
            return [ToolSchemaPolicyTests._contract_shape(item) for item in value]
        return value

    @staticmethod
    def _tools(schema_name):
        schema_path = Path(__file__).resolve().parents[1] / "assets" / schema_name
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        return {item["function"]["name"]: item for item in schema}

    def test_schema_variants_have_matching_types_and_parameters_for_all_tools(self):
        default_tools = self._tools("tools_schema.json")
        chinese_tools = self._tools("tools_schema_cn.json")

        self.assertEqual(len(default_tools), 21)
        self.assertIn("session_search", default_tools)
        self.assertIn("skill_propose", default_tools)
        self.assertEqual(len(chinese_tools), 21)
        self.assertEqual(set(default_tools), set(chinese_tools))
        for tool_name in default_tools:
            with self.subTest(tool_name=tool_name):
                self.assertEqual(
                    default_tools[tool_name]["type"],
                    chinese_tools[tool_name]["type"],
                )
                self.assertEqual(
                    self._contract_shape(
                        default_tools[tool_name]["function"]["parameters"]
                    ),
                    self._contract_shape(
                        chinese_tools[tool_name]["function"]["parameters"]
                    ),
                )

    def test_external_capability_descriptions_document_policy_environment(self):
        schemas = (
            self._tools("tools_schema.json"),
            self._tools("tools_schema_cn.json"),
        )
        for tools in schemas:
            with self.subTest(schema_tools=set(tools)):
                self.assertIn(
                    "GENERICAGENT_MCP_ALLOWLIST",
                    tools["mcp_call"]["function"]["description"],
                )
                self.assertIn(
                    "GENERICAGENT_QQ_ADMIN_ENABLED",
                    tools["qq_group_op"]["function"]["description"],
                )

        for tool_name in ("mcp_call", "qq_group_op"):
            self.assertEqual(
                schemas[0][tool_name]["function"]["parameters"],
                schemas[1][tool_name]["function"]["parameters"],
            )


if __name__ == "__main__":
    unittest.main()
