from pathlib import Path
import os
import tempfile
import unittest

from session_store import ConversationIdentity, SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name, "sessions.db")
        self.store = SessionStore(self.path)
        self.qq = ConversationIdentity("qq", "bot-a", "chat-1", "user-1")

    def tearDown(self):
        self.temp.cleanup()

    def test_wal_and_private_mode(self):
        import sqlite3, stat
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_chinese_trigram_search_is_conversation_scoped(self):
        other = ConversationIdentity("wechat", "bot-a", "chat-1", "user-1")
        self.store.record_exchange(self.qq, 0, "one", "检查操作系统", "Ubuntu 正常")
        self.store.record_exchange(other, 0, "two", "检查操作系统", "另一平台秘密")
        results = self.store.search(self.qq, "操作系统")
        self.assertEqual(len(results), 1)
        self.assertNotIn("另一平台", results[0]["content"])

    def test_generation_and_idempotent_restart_recovery(self):
        self.store.record_exchange(self.qq, 2, "same", "问题", "回答")
        self.store.record_exchange(self.qq, 2, "same", "问题", "回答")
        reopened = SessionStore(self.path)
        self.assertEqual(reopened.recent(self.qq, 2), [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
        ])

    def test_sensitive_messages_are_not_persisted_verbatim(self):
        self.store.record_exchange(self.qq, 0, "secret", "api_key=abcdefghijklmnop", "ok")
        self.assertEqual(self.store.recent(self.qq, 0)[0]["content"], "[sensitive content omitted]")


if __name__ == "__main__":
    unittest.main()
