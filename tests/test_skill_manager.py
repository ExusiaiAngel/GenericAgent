from pathlib import Path
import tempfile
import unittest

from session_store import ConversationIdentity
from skill_manager import SkillManager


class SkillManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = SkillManager(root / "proposals", root / "skills")
        self.owner = ConversationIdentity("qq", "bot", "chat", "user")

    def tearDown(self):
        self.temp.cleanup()

    def test_proposal_is_inactive_until_exact_owner_approves(self):
        proposal = self.manager.propose("unit-skill", "# Unit Skill\nSafe steps.", "test", self.owner)
        self.assertFalse((self.manager.install_root / "unit-skill").exists())
        with self.assertRaises(PermissionError):
            self.manager.approve(proposal["id"], ConversationIdentity("qq", "bot", "chat", "other"))
        approved = self.manager.approve(proposal["id"], self.owner)
        self.assertEqual(approved["status"], "approved")
        self.assertTrue((self.manager.install_root / "unit-skill" / "SKILL.md").is_file())

    def test_secret_and_unsafe_slug_are_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.propose("../escape", "# Bad", "", self.owner)
        with self.assertRaisesRegex(ValueError, "secret"):
            self.manager.propose("bad", "# Bad\napi_key=abcdefghijklmnop", "", self.owner)

    def test_reapproval_is_rejected(self):
        proposal = self.manager.propose("once", "# Once", "", self.owner)
        self.manager.approve(proposal["id"], self.owner)
        with self.assertRaises(ValueError):
            self.manager.approve(proposal["id"], self.owner)


if __name__ == "__main__":
    unittest.main()
