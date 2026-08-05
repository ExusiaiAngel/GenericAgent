import asyncio
import json
import queue
import unittest

from frontends.shared import ipc_server
from frontends.shared.ipc_server import IpcServer


class FakeWriter:
    def __init__(self):
        self.payloads = []
        self.closed = False
        self.waited_closed = False

    def write(self, data):
        self.payloads.append(json.loads(data.decode("utf-8")))

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited_closed = True


class EofReader:
    async def readline(self):
        return b""


class FullQueue:
    def qsize(self):
        return 64

    def put(self, item):
        raise queue.Full

    def put_nowait(self, item):
        raise queue.Full


class FakeAgent:
    def __init__(self):
        self.task_queue = queue.Queue()
        self.session_histories = {}
        self.session_generations = {}
        self.is_running = False
        self.active_task = None
        self.aborted = False

    def reset_chat_session(self, chat_id):
        value = self.session_generations.get(chat_id, 0) + 1
        self.session_generations[chat_id] = value
        return value

    def abort(self):
        self.aborted = True

    def abort_chat_task(self, chat_id):
        if not self.active_task or str(self.active_task.get("chat_id")) != str(chat_id):
            return False
        self.aborted = True
        return True

    def task_status(self, chat_id):
        if self.active_task:
            return {"text": "正在运行 60s（第 4 轮）：生成报告", "state": "running"}
        return {"text": f"{chat_id}: idle", "state": "idle"}


class FakeSessionStore:
    def __init__(self, rows):
        self.rows = list(rows)

    def recent(self, identity, generation, limit=20):
        return self.rows[-limit:]


class RequestProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_full_text_replays_last_assistant_response_without_model_queue(self):
        exact = "第一行\n第二行\n第三行"
        agent = FakeAgent()
        agent.session_store = FakeSessionStore([
            {"role": "user", "content": "生成报告"},
            {"role": "assistant", "content": exact},
        ])
        server = IpcServer(agent)
        writer = FakeWriter()

        await server._process_request(
            {
                "id": "resend-1", "content": "发送全文",
                "platform": "qq", "account_id": "bot", "conversation_id": "group",
                "actor_id": "user", "is_group": True,
            },
            None,
            writer,
        )

        self.assertEqual(agent.task_queue.qsize(), 0)
        self.assertEqual(writer.payloads, [{
            "type": "completed", "id": "resend-1", "text": exact, "error": "",
        }])

    async def test_full_agent_queue_returns_busy_terminal_without_blocking(self):
        agent = FakeAgent()
        agent.task_queue = FullQueue()
        server = IpcServer(agent)
        writer = FakeWriter()

        await server._process_request(
            {"id": "busy-1", "content": "请生成报告", "chat_id": "chat-a"},
            None,
            writer,
        )

        self.assertEqual(writer.payloads[-1]["type"], "failed")
        self.assertIn("繁忙", writer.payloads[-1]["error"])

    async def test_connection_writer_is_closed_and_awaited_on_eof(self):
        server = IpcServer(FakeAgent())
        writer = FakeWriter()

        await server._handle(EofReader(), writer)

        self.assertTrue(writer.closed)
        self.assertTrue(writer.waited_closed)

    async def test_request_emits_accepted_started_and_completed(self):
        agent = FakeAgent()
        agent.is_running = True
        server = IpcServer(agent)
        writer = FakeWriter()
        running = asyncio.create_task(server._process_request(
            {"id": "life-1", "content": "生成一份完整报告", "chat_id": "chat-a"},
            None,
            writer,
        ))
        for _ in range(50):
            if agent.task_queue.qsize():
                break
            await asyncio.sleep(0.001)
        queued = agent.task_queue.get_nowait()
        queued["output"].put({"started": {"turn": 0}})
        queued["output"].put({
            "done": "报告完成",
            "terminal": {
                "state": "completed", "text": "报告完成", "error": "",
                "question": "", "candidates": [],
            },
        })
        await asyncio.wait_for(running, timeout=2)
        types = [payload["type"] for payload in writer.payloads]
        self.assertEqual(types[0], "accepted")
        self.assertIn("started", types)
        self.assertEqual(types[-1], "completed")

    async def test_progress_query_does_not_enter_task_queue(self):
        agent = FakeAgent()
        agent.active_task = {"chat_id": "chat-a", "query": "生成报告", "turn": 4}
        agent.is_running = True
        server = IpcServer(agent)
        writer = FakeWriter()
        await server._process_request(
            {"id": "progress-1", "content": "完成了吗", "chat_id": "chat-a"},
            None,
            writer,
        )
        self.assertEqual(agent.task_queue.qsize(), 0)
        self.assertIn("运行 60s", writer.payloads[-1]["text"])

    async def test_new_is_local_and_does_not_queue_model_task(self):
        agent = FakeAgent()
        server = IpcServer(agent)
        writer = FakeWriter()
        await server._process_request(
            {"id": "new-1", "content": "/new", "chat_id": "chat-a"},
            None,
            writer,
        )
        self.assertEqual(agent.task_queue.qsize(), 0)
        self.assertEqual(writer.payloads[-1]["type"], "completed")
        self.assertIn("上下文已清空", writer.payloads[-1]["text"])

    async def test_ambiguous_confirmation_repeats_question_without_queueing(self):
        agent = FakeAgent()
        server = IpcServer(agent)
        server.pending_inputs["chat-a"] = {
            "question": "是否允许修改 memory/?",
            "candidates": ["允许", "拒绝"],
            "created_at": server.clock(),
        }
        writer = FakeWriter()
        await server._process_request(
            {"id": "ask-1", "content": "完成了吗", "chat_id": "chat-a"},
            None,
            writer,
        )
        self.assertEqual(agent.task_queue.qsize(), 0)
        self.assertEqual(writer.payloads[-1]["type"], "needs_input")
        self.assertIn("是否允许", writer.payloads[-1]["question"])

    async def test_status_is_local(self):
        agent = FakeAgent()
        server = IpcServer(agent)
        writer = FakeWriter()
        await server._process_request(
            {"id": "status-1", "content": "/status", "chat_id": "chat-a"},
            None,
            writer,
        )
        self.assertEqual(agent.task_queue.qsize(), 0)
        self.assertIn("idle", writer.payloads[-1]["text"])

    async def test_stop_cannot_abort_another_chat(self):
        agent = FakeAgent()
        agent.is_running = True
        agent.active_task = {"chat_id": "chat-b", "generation": 3}
        server = IpcServer(agent)
        writer = FakeWriter()

        await server._process_request(
            {"id": "stop-a", "content": "/stop", "chat_id": "chat-a"},
            None,
            writer,
        )

        self.assertFalse(agent.aborted)
        self.assertIn("当前会话", writer.payloads[-1]["text"])

    async def test_stop_aborts_only_the_requesting_chat(self):
        agent = FakeAgent()
        agent.is_running = True
        agent.active_task = {"chat_id": "chat-a", "generation": 3}
        server = IpcServer(agent)
        writer = FakeWriter()

        await server._process_request(
            {"id": "stop-a", "content": "/stop", "chat_id": "chat-a"},
            None,
            writer,
        )

        self.assertTrue(agent.aborted)
        self.assertIn("已请求停止", writer.payloads[-1]["text"])


class CacheIsolationTests(unittest.TestCase):
    def tearDown(self):
        ipc_server._CACHE.clear()

    def test_cache_isolated_by_chat_generation_and_route(self):
        ipc_server._set_cache("你好", "reply-a", "chat-a", 1, "quick_chat")
        self.assertEqual(
            ipc_server._check_cache("你好", "chat-a", 1, "quick_chat"),
            "reply-a",
        )
        self.assertIsNone(ipc_server._check_cache("你好", "chat-b", 1, "quick_chat"))
        self.assertIsNone(ipc_server._check_cache("你好", "chat-a", 2, "quick_chat"))
        self.assertIsNone(ipc_server._check_cache("你好", "chat-a", 1, "long_task"))
