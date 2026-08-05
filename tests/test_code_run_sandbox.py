import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ga import GenericAgentHandler, code_run
from tests.test_tool_safety import exhaust_with_output


@unittest.skipUnless(os.name == "posix", "Bubblewrap integration is Linux-only")
class CodeRunSandboxTests(unittest.TestCase):
    def test_ipc_inline_eval_is_denied(self):
        handler = GenericAgentHandler(
            SimpleNamespace(),
            cwd=os.getcwd(),
            allow_inline_eval=False,
        )
        outcome, _ = exhaust_with_output(
            handler.do_code_run(
                {
                    "type": "python",
                    "inline_eval": True,
                    "script": "open('/tmp/escape','w').write('x')",
                },
                SimpleNamespace(content=""),
            )
        )
        self.assertEqual(outcome.data["status"], "error")
        self.assertIn("inline_eval", outcome.data["msg"])

    def test_bash_cannot_write_outside_configured_root(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            allowed = base / "allowed"
            outside = base / "outside.txt"
            allowed.mkdir()
            with patch.dict(os.environ, {"GENERICAGENT_WRITE_ROOTS": str(allowed)}):
                result, _ = exhaust_with_output(
                    code_run(
                        f"printf denied > {outside}",
                        "bash",
                        timeout=10,
                        cwd=str(base),
                    )
                )
            self.assertEqual(result["status"], "error")
            self.assertFalse(outside.exists())

    def test_bash_can_write_inside_configured_root(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            allowed = base / "allowed"
            target = allowed / "ok.txt"
            allowed.mkdir()
            with patch.dict(os.environ, {"GENERICAGENT_WRITE_ROOTS": str(allowed)}):
                result, _ = exhaust_with_output(
                    code_run(
                        f"printf ok > {target}",
                        "bash",
                        timeout=10,
                        cwd=str(base),
                    )
                )
            self.assertEqual(result["status"], "success")
            self.assertEqual(target.read_text(encoding="utf-8"), "ok")

    def test_python_resolves_to_project_virtualenv(self):
        result, _ = exhaust_with_output(
            code_run(
                "command -v python",
                "bash",
                timeout=10,
                cwd="/opt/GenericAgent/temp",
            )
        )
        self.assertEqual(result["status"], "success")
        self.assertIn("/opt/GenericAgent/venv/bin/python", result["stdout"])

    def test_pipeline_propagates_upstream_failure(self):
        result, _ = exhaust_with_output(
            code_run(
                "false | tail -1",
                "bash",
                timeout=10,
                cwd="/opt/GenericAgent/temp",
            )
        )
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result["exit_code"], 0)

