import tempfile
import unittest
from pathlib import Path

from llmcore import _redact_llm_log, _write_llm_log


class LlmLogRedactionTests(unittest.TestCase):
    def test_secret_shapes_are_removed_but_normal_text_survives(self):
        api_key = "sk-unit-secret-1234567890"
        app_secret = "unit-app-secret-value"
        content = f'normal text\napi_key="{api_key}"\nqq_appsecret={app_secret}'

        redacted = _redact_llm_log(content)

        self.assertIn("normal text", redacted)
        self.assertNotIn(api_key, redacted)
        self.assertNotIn(app_secret, redacted)
        self.assertIn("[REDACTED", redacted)

    def test_writer_never_persists_secret_and_sets_private_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "model.log"
            _write_llm_log("Prompt", "token=unit-token-value", path)
            content = path.read_text(encoding="utf-8")

            self.assertNotIn("unit-token-value", content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
