import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config_check


class ConfigCheckIsolationTests(unittest.TestCase):
    def test_private_env_loader_is_simple_non_overwriting_and_secret_safe(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text(
                "# comment\nEXISTING=replaced\nexport NEW_KEY='private-value'\n"
                "INVALID-NAME=ignored\nNO_EQUALS\n",
                encoding="utf-8",
            )
            environ = {"EXISTING": "original"}

            loaded = config_check.load_private_env(path, environ)

        self.assertEqual(loaded, ("NEW_KEY",))
        self.assertEqual(environ["EXISTING"], "original")
        self.assertEqual(environ["NEW_KEY"], "private-value")
        self.assertNotIn("INVALID-NAME", environ)

    @mock.patch("config_check.subprocess.run")
    def test_agent_check_runs_in_isolated_process(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="GenericAgent\n", stderr=""
        )

        status, message = config_check.check_agent()

        self.assertEqual(status, "pass")
        self.assertEqual(message, "GenericAgent (GenericAgent)")
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], config_check.sys.executable)
        self.assertEqual(kwargs["cwd"], config_check.PROJECT_ROOT)
        self.assertIn("os._exit(0)", args[0][2])

    @mock.patch("config_check.subprocess.run")
    def test_agent_check_reports_timeout(self, run):
        run.side_effect = subprocess.TimeoutExpired(cmd=["python"], timeout=20)

        status, message = config_check.check_agent()

        self.assertEqual(status, "fail")
        self.assertIn("timed out", message)


if __name__ == "__main__":
    unittest.main()
