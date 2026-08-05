from frontends.shared.chatapp_common import format_for_chat
import asyncio
import inspect
import json
import os
import stat
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from frontends.chat import napcat_qqapp
from frontends.chat.napcat_qqapp import NapCatApp, _compact_health_report, _compact_result_report
from frontends.shared.chatapp_common import _open_private_log


class NapCatReplyQualityTests(unittest.IsolatedAsyncioTestCase):
    def test_message_event_log_excludes_content_and_direct_identifiers(self):
        line = napcat_qqapp._safe_message_event(
            "group mention",
            user_id="99999",
            chat_id="88888",
            content="secret prompt from user",
            action="status",
        )

        self.assertNotIn("secret prompt", line)
        self.assertNotIn("99999", line)
        self.assertNotIn("88888", line)
        self.assertIn("chars=23", line)

    async def test_frontend_heartbeat_detects_stale_or_wrong_process(self):
        from scripts.frontend_heartbeat import check_heartbeat, write_heartbeat

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "frontend.heartbeat")
            write_heartbeat(path, now=100.0, pid=1234)
            fresh, _ = check_heartbeat(
                path, now=105.0, max_age=10.0, expected_pid=1234
            )
            stale, _ = check_heartbeat(
                path, now=120.0, max_age=10.0, expected_pid=1234
            )
            wrong_pid, _ = check_heartbeat(
                path, now=105.0, max_age=10.0, expected_pid=9999
            )

        self.assertTrue(fresh)
        self.assertFalse(stale)
        self.assertFalse(wrong_pid)

    async def test_group_sender_must_be_allowlisted_before_commands_run(self):
        app = NapCatApp()
        app.self_id = "4242"
        app.handle_command = AsyncMock()
        event = {
            "message_id": "blocked-group-command",
            "group_id": 90001,
            "raw_message": "[CQ:at,qq=4242] /status",
            "sender": {"user_id": 99999, "nickname": "untrusted"},
        }

        with patch.object(napcat_qqapp, "ALLOWED", {"12345"}):
            await app.on_message(event, is_group=True)

        app.handle_command.assert_not_awaited()

    def test_progress_uses_total_elapsed(self):
        app = NapCatApp()
        self.assertIn("125s", app._progress_text(125, "有新进展"))
        self.assertIn("65s", app._progress_text(65, ""))

    def test_async_send_path_has_no_blocking_sleep(self):
        source = inspect.getsource(napcat_qqapp)
        self.assertNotIn("time.sleep(delay)", source)

    async def test_send_text_returns_false_when_ws_missing(self):
        app = NapCatApp()
        app.ws = None
        ok = await app.send_text("511925693", "hello", is_group=True)
        self.assertFalse(ok)

    async def test_send_text_returns_false_on_send_msg_failure(self):
        app = NapCatApp()

        async def fake_call_action(action, params, timeout=15):
            return {"status": "failed", "wording": "blocked"}

        app._call_action = fake_call_action
        ok = await app.send_text("511925693", "hello", is_group=True)
        self.assertFalse(ok)

    async def test_send_text_returns_true_when_all_parts_send(self):
        app = NapCatApp()
        sent = []

        async def fake_call_action(action, params, timeout=15):
            sent.append((action, params))
            return {"status": "ok"}

        app._call_action = fake_call_action
        ok = await app.send_text("511925693", "hello", is_group=True)
        self.assertTrue(ok)
        self.assertEqual(sent[0][0], "send_msg")

    async def test_send_text_accepts_command_operator_context(self):
        app = NapCatApp()

        async def fake_call_action(action, params, timeout=15):
            return {"status": "ok"}

        app._call_action = fake_call_action
        ok = await app.send_text(
            "511925693",
            "help text",
            is_group=True,
            user_id="2835429039",
        )
        self.assertTrue(ok)

    async def test_push_ack_sent_after_push_delivery(self):
        app = NapCatApp()
        writes = []

        class Writer:
            def write(self, data):
                writes.append(data.decode("utf-8"))
            async def drain(self):
                return None

        app.ipc_writer = Writer()

        async def fake_send_text(chat_id, content, **ctx):
            return True

        app.send_text = fake_send_text
        await app._handle_push({
            "type": "push",
            "push_id": "push_unit_ok",
            "chat_id": "511925693",
            "is_group": True,
            "text": "done",
        })
        ack = json.loads(writes[0])
        self.assertEqual(ack["type"], "push_ack")
        self.assertEqual(ack["push_id"], "push_unit_ok")
        self.assertTrue(ack["ok"])

    async def test_run_agent_shows_visible_progress_even_when_chunks_arrive(self):
        old_first = napcat_qqapp.VISIBLE_PROGRESS_FIRST_SEC
        old_repeat = napcat_qqapp.VISIBLE_PROGRESS_REPEAT_SEC
        old_port = napcat_qqapp.IPC_PORT
        napcat_qqapp.VISIBLE_PROGRESS_FIRST_SEC = 0.2
        napcat_qqapp.VISIBLE_PROGRESS_REPEAT_SEC = 0.2
        napcat_qqapp.IPC_PORT = 19191

        async def fake_ipc(reader, writer):
            raw = await reader.readline()
            req = json.loads(raw.decode("utf-8"))
            rid = req["id"]
            for i in range(3):
                writer.write((json.dumps({"type": "chunk", "id": rid, "text": f"internal {i}"}) + "\n").encode("utf-8"))
                await writer.drain()
                await asyncio.sleep(0.12)
            writer.write((json.dumps({"type": "done", "id": rid, "text": "最终回复"}, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(fake_ipc, "127.0.0.1", 19191)
        app = NapCatApp()
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        app.send_text = fake_send
        try:
            await app.run_agent("511925693", "slow task", is_group=True)
        finally:
            server.close()
            await server.wait_closed()
            napcat_qqapp.VISIBLE_PROGRESS_FIRST_SEC = old_first
            napcat_qqapp.VISIBLE_PROGRESS_REPEAT_SEC = old_repeat
            napcat_qqapp.IPC_PORT = old_port

        self.assertEqual(sent[0], "思考中...")
        self.assertTrue(any("还在处理" in x for x in sent))
        self.assertEqual(sent[-1], "最终回复")

    async def test_run_agent_max_turns_sends_error_not_partial_action(self):
        old_port = napcat_qqapp.IPC_PORT
        napcat_qqapp.IPC_PORT = 19192

        async def fake_ipc(reader, writer):
            raw = await reader.readline()
            req = json.loads(raw.decode("utf-8"))
            rid = req["id"]
            messages = [
                {"type": "accepted", "id": rid, "state": "starting", "queue_position": 0},
                {"type": "started", "id": rid, "task": {}},
                {"type": "chunk", "id": rid, "text": "尝试抓取其他站点。"},
                {
                    "type": "max_turns", "id": rid, "text": "",
                    "error": "任务在 6 个模型回合后仍未完成，已安全停止。",
                },
            ]
            for message in messages:
                writer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(fake_ipc, "127.0.0.1", 19192)
        app = NapCatApp()
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        app.send_text = fake_send
        try:
            await app.run_agent("511925693", "long task", is_group=True)
        finally:
            server.close()
            await server.wait_closed()
            napcat_qqapp.IPC_PORT = old_port

        self.assertTrue(any("安全停止" in text for text in sent))
        self.assertFalse(any("尝试抓取" in text for text in sent))

    async def test_health_command_stays_local(self):
        app = NapCatApp()
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_health():
            return "服务器健康快照 2026-07-08 20:00:00\n[OK] Scheduler"

        app.send_text = fake_send
        app._build_health_report = fake_health
        await app.handle_command("511925693", "/health", is_group=True)
        self.assertEqual(len(sent), 1)
        self.assertIn("服务器健康快照", sent[0])
        self.assertIn("[OK] Scheduler", sent[0])

    async def test_task_submit_command_uses_assistantctl_submit(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append(args)
            return 0, "/opt/GenericAgent/sandbox/inbox/20260708_202200_manual.md"

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/task 生成一份服务器周报", is_group=True)
        self.assertEqual(calls, [["submit", "生成一份服务器周报"]])
        self.assertEqual(len(sent), 1)
        self.assertIn("已加入远程助手队列", sent[0])
        self.assertIn("sandbox/inbox/20260708_202200_manual.md", sent[0])

    async def test_task_submit_requires_body(self):
        app = NapCatApp()
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        app.send_text = fake_send
        await app.handle_command("511925693", "/task", is_group=True)
        self.assertIn("用法: /task", sent[0])

    async def test_tasks_and_result_commands_use_fixed_assistantctl_args(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append((args, timeout))
            if args == ["pending"]:
                return 0, "pending=0\nactive=0"
            if args == ["results", "--latest", "--tail", "80"]:
                return 0, "# Result\nall good"
            return 1, "unexpected"

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/tasks", is_group=True)
        await app.handle_command("511925693", "/result", is_group=True)
        self.assertEqual(calls[0][0], ["pending"])
        self.assertEqual(calls[1][0], ["results", "--latest", "--tail", "80"])
        self.assertIn("远程助手队列", sent[0])
        self.assertIn("最近远程任务结果", sent[1])

    async def test_tasknow_command_uses_submit_now(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append((args, timeout))
            return 0, "sandbox/inbox/20260708_202900_manual.md\n[2026-07-08 20:29:00] launched inbox_task"

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/tasknow 生成一份远程状态摘要", is_group=True)
        self.assertEqual(calls[0][0], ["submit", "--now", "生成一份远程状态摘要"])
        self.assertGreaterEqual(calls[0][1], 60)
        self.assertIn("已提交并触发远程助手处理", sent[0])

    async def test_run_command_uses_run_inbox(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append((args, timeout))
            return 0, ""

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/run", is_group=True)
        self.assertEqual(calls[0][0], ["run-inbox"])
        self.assertGreaterEqual(calls[0][1], 60)
        self.assertIn("已触发远程助手队列扫描", sent[0])
        self.assertIn("没有待处理任务", sent[0])

    async def test_alerts_command_uses_fixed_assistantctl_args(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append(args)
            return 0, "alerts=0"

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/alerts", is_group=True)
        self.assertEqual(calls, [["alerts", "--verbose", "--limit", "10"]])
        self.assertIn("远程助手告警", sent[0])
        self.assertIn("alerts=0", sent[0])

    async def test_doctor_command_uses_fixed_assistantctl_args(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append(args)
            return 0, "overall=READY\nnext_actions:\n  - genericagent-assistant submit --now \"你的任务\""

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/doctor", is_group=True)
        self.assertEqual(calls, [["doctor"]])
        self.assertIn("远程助手诊断", sent[0])
        self.assertIn("overall=READY", sent[0])
        self.assertIn("next_actions", sent[0])

    async def test_dashboard_command_uses_fixed_assistantctl_args(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append(args)
            return 0, "# GenericAgent Assistant Dashboard\n\n- overall: `READY`"

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/dashboard", is_group=True)
        self.assertEqual(calls, [["dashboard", "--tail", "80"]])
        self.assertIn("远程助手驾驶舱", sent[0])
        self.assertIn("overall: `READY`", sent[0])

    async def test_audit_commands_use_fixed_assistantctl_args(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append(args)
            if args == ["audits", "--latest", "--tail", "120"]:
                return 0, "# Inbox Supervision\n\n- verdict: `PASS`"
            if args == ["audits", "--limit", "8"]:
                return 0, "PASS\tinbox_task\tsandbox/reports/inbox_audits/inbox_task.json"
            return 1, "unexpected"

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/audit", is_group=True)
        await app.handle_command("511925693", "/audits", is_group=True)
        self.assertEqual(calls[0], ["audits", "--latest", "--tail", "120"])
        self.assertEqual(calls[1], ["audits", "--limit", "8"])
        self.assertIn("最近远程任务审计", sent[0])
        self.assertIn("verdict: `PASS`", sent[0])
        self.assertIn("远程任务审计列表", sent[1])
        self.assertIn("PASS", sent[1])

    async def test_logs_command_is_private_only(self):
        app = NapCatApp()
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        app.send_text = fake_send
        await app.handle_command("511925693", "/logs inbox 20", is_group=True)
        self.assertIn("请私聊使用", sent[0])

    async def test_logs_command_uses_whitelisted_source_and_clamps_lines(self):
        app = NapCatApp()
        calls = []
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        async def fake_assistantctl(args, timeout=35):
            calls.append(args)
            return 0, "line1\nline2"

        app.send_text = fake_send
        app._run_assistantctl = fake_assistantctl
        await app.handle_command("511925693", "/logs inbox 999", is_group=False)
        self.assertEqual(calls, [["logs", "inbox", "--lines", "80"]])
        self.assertIn("inbox 日志最近 80 行", sent[0])
        self.assertIn("line1", sent[0])

    async def test_logs_command_rejects_unknown_source(self):
        app = NapCatApp()
        sent = []

        async def fake_send(chat_id, content, **ctx):
            sent.append(content)
            return True

        app.send_text = fake_send
        await app.handle_command("511925693", "/logs /etc/passwd 20", is_group=False)
        self.assertIn("用法: /logs", sent[0])


class HealthReportFormattingTests(unittest.TestCase):
    def test_compact_health_report_skips_log_tail(self):
        raw = """==============================
 GenericAgent Status Report
Time: 2026-07-08 20:00:00

[OK] Scheduler
Assistant dashboard: /opt/GenericAgent/sandbox/reports/assistant_dashboard.md
- overall: `READY`
-- Logs --
[NapCat] last 3:
  noisy log line
Memory: 548Mi/1.6Gi available=1.0Gi
=============================="""

        out = _compact_health_report(raw)
        self.assertIn("服务器健康快照 2026-07-08 20:00:00", out)
        self.assertIn("[OK] Scheduler", out)
        self.assertIn("- overall: `READY`", out)
        self.assertNotIn("Assistant dashboard:", out)
        self.assertNotIn("noisy log line", out)

    def test_compact_result_report_hides_raw_output(self):
        raw = """# Inbox Result: inbox_example

- status: `completed`
- supervision: `PASS`

## Output

Turn 1 ...
tool_call({\"secretish\":\"noise\"})
final answer"""

        out = _compact_result_report(raw)
        self.assertIn("# Inbox Result: inbox_example", out)
        self.assertIn("supervision: `PASS`", out)
        self.assertIn("完整输出已隐藏", out)
        self.assertNotIn("Turn 1", out)
        self.assertNotIn("tool_call", out)


class ChatFormattingTests(unittest.TestCase):
    def test_format_for_chat_handles_unclosed_agent_fence_in_linear_time(self):
        raw = "````text\n" + "\n".join(
            f"streamed tool output line {i}: " + ("x" * 48)
            for i in range(60)
        )

        started = time.perf_counter()
        out = format_for_chat(raw, is_group=True)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.20)
        self.assertIn("没有生成可发送的最终答案", out)

    def test_private_log_open_enforces_0640(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "runtime.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("old")
            os.chmod(path, 0o644)

            logf = _open_private_log(path)
            logf.write("new")
            logf.close()

            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)

    def test_format_for_chat_compacts_long_report(self):
        raw = "# Report\n\n" + "\n".join(f"- detail line {i}" for i in range(80))
        out = format_for_chat(raw, is_group=True, max_chars=360)
        self.assertLessEqual(len(out), 420)
        self.assertIn("已省略", out)

    def test_format_for_chat_keeps_final_answer_after_verbose_tool_turn(self):
        raw = """

**LLM Running (Turn 1) ...**

<summary>先检查当前状态</summary>

🛠️ Tool: `code_run`  📥 args:
````text
{"script": "print('status')"}
````
`````
[Status] ✅ Exit Code: 0
[Stdout]
status
`````

**LLM Running (Turn 2) ...**

功能：我能处理文件、搜索、代码和自动化任务。
状态：在线，当前空闲。

`````
[Info] Final response to user.
`````
"""

        out = format_for_chat(raw, is_group=True)

        self.assertIn("功能：我能处理文件", out)
        self.assertIn("状态：在线", out)
        self.assertNotIn("处理完成", out)
        self.assertNotIn("Exit Code", out)

    def test_format_for_chat_reports_missing_visible_answer_truthfully(self):
        out = format_for_chat("<summary>只有内部摘要</summary>", is_group=True)

        self.assertIn("没有生成可发送的最终答案", out)
        self.assertNotEqual(out, "处理完成。")


if __name__ == "__main__":
    unittest.main()
