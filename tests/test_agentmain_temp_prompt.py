import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor


class LongPromptFileTests(unittest.TestCase):
    def test_concurrent_long_prompts_get_unique_atomic_files(self):
        from agentmain import _save_long_user_prompt

        payloads = [f"prompt-{index}" for index in range(24)]
        with tempfile.TemporaryDirectory() as td:
            with ThreadPoolExecutor(max_workers=8) as pool:
                paths = list(pool.map(lambda text: _save_long_user_prompt(text, td), payloads))

            self.assertEqual(len(set(paths)), len(payloads))
            self.assertTrue(all(os.path.dirname(path) == td for path in paths))
            loaded = []
            for path in paths:
                with open(path, encoding="utf-8") as handle:
                    loaded.append(handle.read())
            self.assertCountEqual(loaded, payloads)


if __name__ == "__main__":
    unittest.main()
