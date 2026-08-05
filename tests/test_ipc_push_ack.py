import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from frontends.shared import ipc_server


class DummyAgent:
    session_histories = {}
    session_configs = {}


class FakeWriter:
    def __init__(self):
        self.payloads = []
        self.closed = False

    def write(self, data):
        self.payloads.append(data.decode("utf-8"))

    async def drain(self):
        return None


class IpcPushAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_returns_false_without_registered_napcat(self):
        server = ipc_server.IpcServer(DummyAgent())
        ok = await server._push("511925693", True, "hello", push_id="unit_no_writer", ack_timeout=0.05)
        self.assertFalse(ok)

    async def test_push_returns_true_when_ack_arrives(self):
        server = ipc_server.IpcServer(DummyAgent())
        writer = FakeWriter()
        server._push_writer = writer

        async def ack_soon():
            await asyncio.sleep(0.01)
            await server._handle_push_ack({"type": "push_ack", "push_id": "unit_ok", "ok": True})

        asyncio.create_task(ack_soon())
        ok = await server._push("511925693", True, "hello", push_id="unit_ok", ack_timeout=0.2)
        self.assertTrue(ok)
        payload = json.loads(writer.payloads[0])
        self.assertEqual(payload["type"], "push")
        self.assertEqual(payload["push_id"], "unit_ok")
        self.assertEqual(payload["chat_id"], "511925693")

    async def test_push_returns_false_when_ack_reports_send_failure(self):
        server = ipc_server.IpcServer(DummyAgent())
        writer = FakeWriter()
        server._push_writer = writer

        async def ack_soon():
            await asyncio.sleep(0.01)
            await server._handle_push_ack({
                "type": "push_ack",
                "push_id": "unit_fail",
                "ok": False,
                "error": "send_msg timeout",
            })

        asyncio.create_task(ack_soon())
        ok = await server._push("511925693", True, "hello", push_id="unit_fail", ack_timeout=0.2)
        self.assertFalse(ok)

    def test_mark_push_failure_keeps_pushed_false_and_schedules_retry(self):
        with tempfile.TemporaryDirectory() as td:
            meta_file = Path(td) / ".ipc_meta.json"
            meta = {"chat_id": "511925693", "is_group": True, "pushed": False}
            ipc_server._mark_push_failure(meta_file, meta, "send_msg timeout", now=1000)
            saved = json.loads(meta_file.read_text(encoding="utf-8"))
            self.assertFalse(saved["pushed"])
            self.assertEqual(saved["push_attempts"], 1)
            self.assertEqual(saved["push_last_error"], "send_msg timeout")
            self.assertGreaterEqual(saved["next_retry_at"], 1005)

    def test_mark_push_success_sets_delivery_fields(self):
        with tempfile.TemporaryDirectory() as td:
            meta_file = Path(td) / ".ipc_meta.json"
            meta = {"chat_id": "511925693", "is_group": True, "pushed": False, "push_attempts": 2}
            ipc_server._mark_push_success(meta_file, meta, push_id="unit_success", now=2000)
            saved = json.loads(meta_file.read_text(encoding="utf-8"))
            self.assertTrue(saved["pushed"])
            self.assertEqual(saved["push_id"], "unit_success")
            self.assertEqual(saved["push_last_ok_at"], 2000)
            self.assertEqual(saved["push_last_error"], "")


if __name__ == "__main__":
    unittest.main()
