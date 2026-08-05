import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


STAGE_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


salient_mining = load_module(
    "salient_mining_under_test",
    STAGE_ROOT / "memory" / "L4_raw_sessions" / "salient_mining.py",
)
memory_audit = load_module(
    "memory_audit_under_test",
    STAGE_ROOT / "memory" / "memory_audit.py",
)


class SalientMiningPolicyTests(unittest.TestCase):
    def test_user_prompt_mentions_are_not_promoted_to_l2_facts(self):
        facts = salient_mining._extract_facts(
            "0713_1200-0713_1201",
            [
                "请在 WSL 中读取 memory/example_sop.md，"
                "然后 write sandbox report。"
            ],
        )

        self.assertEqual(facts["discoveries"], [])
        self.assertIn("example_sop.md", facts["tools"])


class MemoryAuditTests(unittest.TestCase):
    def test_vector_index_files_schema_is_understood(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Path(temp_dir)
            (memory / "a.md").write_text("a", encoding="utf-8")
            (memory / "vector_index.json").write_text(
                json.dumps(
                    {
                        "files": [{"path": "a.md", "vector": [1.0]}],
                        "model": "sklearn_TfidfVectorizer",
                        "num_files": 1,
                    }
                ),
                encoding="utf-8",
            )

            issues, stats = memory_audit.check_vector_index(memory)

        self.assertEqual(stats["index_entries"], 1)
        self.assertEqual(stats["missing_files"], 0)
        self.assertFalse(
            any(issue["type"] == "index_file_mismatch" for issue in issues)
        )

    def test_reference_audit_uses_syntax_not_docstring_examples(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Path(temp_dir)
            (memory / "example.py").write_text(
                '"""Examples only: from .xxx import y; open("xxx.json")"""\n',
                encoding="utf-8",
            )
            (memory / "broken.py").write_text(
                "from .missing import value\n"
                "with open('missing.json') as handle:\n"
                "    handle.read()\n",
                encoding="utf-8",
            )

            issues, stats = memory_audit.check_sop_tool_references(memory)

        refs = {(issue["type"], issue.get("reference")) for issue in issues}
        self.assertEqual(
            refs,
            {
                ("broken_import", ".missing"),
                ("broken_file_ref", "missing.json"),
            },
        )
        self.assertEqual(stats["broken_refs"], 2)


if __name__ == "__main__":
    unittest.main()
