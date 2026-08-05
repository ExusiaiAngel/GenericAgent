import gc
import hashlib
import hmac
import asyncio, base64, io, json, os, random, re, subprocess, sys, threading, time, traceback
from collections import deque

import aiohttp
from PIL import Image

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
from frontends.shared.chatapp_common import (AgentChatMixin, ensure_single_instance,
    public_access, redirect_log, split_text, extract_files,
    _extract_final_answer, format_for_chat)
from frontends.shared.ipc_server import (
    IPC_PROTOCOL_VERSION, _derive_session_key, _handshake_mac,
    _sign_session_frame, _verify_session_frame, load_ipc_auth_token,
)
from llmcore import mykeys
from scripts.frontend_heartbeat import write_heartbeat

WS_URL   = os.environ.get('NAPCAT_WS', 'ws://127.0.0.1:3001/ws')
ALLOWED  = {str(x).strip() for x in mykeys.get('qq_allowed_users', []) if str(x).strip()}

IPC_HOST = "127.0.0.1"
IPC_PORT = 9001
_RE_CQ = re.compile(r'\[CQ:[^\]]+\]')
HEALTH_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "status.sh")
HEALTH_TIMEOUT = 20
ASSISTANTCTL_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "assistantctl.py")
ASSISTANTCTL_TIMEOUT = 35
ASSISTANTCTL_RUN_TIMEOUT = 90
TASK_TEXT_MAX = 4000
LOG_SOURCES = {"inbox", "watchdog", "napcat"}
LOG_LINES_DEFAULT = 40
LOG_LINES_MAX = 80

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}
_IMAGE_MAX_BATCH = 5  # max images per single reply
VISIBLE_PROGRESS_FIRST_SEC = 12
VISIBLE_PROGRESS_REPEAT_SEC = 30
IPC_READ_TIMEOUT = 10
IPC_IDLE_TIMEOUT = max(30, int(os.environ.get("GENERICAGENT_IPC_IDLE_TIMEOUT", "300")))
IPC_TOTAL_TIMEOUT = max(60, int(os.environ.get("GENERICAGENT_IPC_TOTAL_TIMEOUT", "3600")))


def _privacy_token(value):
    if value in (None, ""):
        return "none"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def _safe_message_event(kind, *, user_id="", chat_id="", content="", action=""):
    safe_action = re.sub(r"[^a-zA-Z0-9_-]", "", str(action or ""))[:32]
    return (
        f"[NapCat] {kind} user={_privacy_token(user_id)} "
        f"chat={_privacy_token(chat_id)} chars={len(str(content or ''))} "
        f"action={safe_action or 'none'}"
    )

# ── Rate limiter ──
_RATE_LIMIT = deque(maxlen=20)  # track last 20 send times
_RATE_WINDOW = 5  # seconds
_RATE_MAX = 8  # max sends per window
_RATE_LOCK = None
_RATE_LOCK_LOOP = None


async def _wait_for_rate_limit():
    global _RATE_LOCK, _RATE_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _RATE_LOCK is None or _RATE_LOCK_LOOP is not loop:
        _RATE_LOCK = asyncio.Lock()
        _RATE_LOCK_LOOP = loop
    async with _RATE_LOCK:
        while True:
            now = time.monotonic()
            while _RATE_LIMIT and now - _RATE_LIMIT[0] >= _RATE_WINDOW:
                _RATE_LIMIT.popleft()
            if len(_RATE_LIMIT) < _RATE_MAX:
                _RATE_LIMIT.append(now)
                return
            delay = max(0.0, _RATE_LIMIT[0] + _RATE_WINDOW - now)
            await asyncio.sleep(delay)


# Dummy agent for AgentChatMixin — all work is delegated to main agent via IPC
class _DummyAgent:
    is_running = False
    llmclient = None
    llm_no = 0
    history = []
    def get_llm_name(self, *a): return "IPC"
    def list_llms(self): return [(0, "IPC → Main Agent", True)]
    def next_llm(self, n): pass
    def abort(self): pass
_dummy_agent = _DummyAgent()

PROCESSED_IDS = deque(maxlen=2000)
USER_TASKS = {}

_pending_echo: dict[str, asyncio.Future] = {}
_pending_ipc: dict[str, asyncio.Future] = {}

def _handle_ws_response(data: dict):
    echo = data.get('echo', '')
    if echo and echo in _pending_echo:
        fut = _pending_echo.pop(echo)
        if not fut.done():
            fut.set_result(data)

def _compress_for_forward(img_bytes: bytes, max_px: int = 800) -> bytes:
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size
    if w > max_px or h > max_px:
        ratio = max_px / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img = img.convert('RGB')
    img.save(buf, 'JPEG', quality=75)
    return buf.getvalue()

def _evade_censor(img_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(img_bytes))
    scale = random.uniform(0.97, 1.0)
    if scale < 1.0:
        w, h = img.size
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img = img.convert('RGB')
    img.save(buf, 'JPEG', quality=random.randint(82, 88))
    return buf.getvalue()

def _is_image_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in _IMAGE_EXTS

def _compact_health_report(raw, max_chars=1400):
    """Keep the server health report short enough for QQ."""
    lines = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or set(s) == {"="}:
            continue
        if s == "GenericAgent Status Report":
            continue
        if s == "-- Logs --":
            break
        if s.startswith("Assistant dashboard:") or s.startswith("Supervisor snapshot:"):
            continue
        if s.startswith("Latest inbox report:") or s.startswith("Latest inbox supervision:"):
            continue
        if s.startswith("Time:"):
            lines.append(f"服务器健康快照 {s[5:].strip()}")
            continue
        lines.append(s)

    if not lines:
        return "服务器健康快照暂时没有可用输出。"

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...已截断，可 SSH 执行 scripts/status.sh 查看完整报告。"

def _compact_operator_output(raw, max_chars=1400):
    text = (raw or "").strip()
    if not text:
        return "(无输出)"
    text = text.replace(PROJECT_ROOT + "/", "")
    text = text.replace(PROJECT_ROOT, ".")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...已截断。"

def _compact_result_report(raw, max_chars=1200):
    text = (raw or "").strip()
    if not text:
        return "(无最近结果)"
    head = text.split("\n## Output", 1)[0].strip()
    if head != text:
        head += "\n\n完整输出已隐藏，避免把内部工具日志发到 QQ；需要时用 SSH 执行 assistantctl results --latest --tail 120。"
    return _compact_operator_output(head, max_chars=max_chars)

class NapCatApp(AgentChatMixin):
    label, source, split_limit = 'NapCatQQ', 'napcatqq', 1500

    def __init__(self, *, auth_token_reader=load_ipc_auth_token):
        super().__init__(_dummy_agent, USER_TASKS)
        self.ws = None
        self.ws_session = None
        self._echo_counter = 0
        self.self_id = None
        self.ipc_reader = None
        self.ipc_writer = None
        self._ipc_connected = False
        self._auth_token_reader = auth_token_reader
        self._ipc_sessions = {}

    @staticmethod
    async def _write_raw_ipc_frame(writer, message):
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        writer.write(payload.encode("utf-8"))
        await writer.drain()

    async def _ipc_handshake(self, reader, writer):
        """Mutually authenticate without disclosing the long-lived token."""
        client_nonce = os.urandom(32).hex()
        await self._write_raw_ipc_frame(writer, {
            "type": "hello", "version": IPC_PROTOCOL_VERSION,
            "client_nonce": client_nonce,
        })
        raw = await asyncio.wait_for(reader.readline(), timeout=10)
        challenge = json.loads(raw.decode("utf-8").strip()) if raw else {}
        server_nonce = (
            challenge.get("server_nonce")
            if isinstance(challenge, dict) else None
        )
        proof = challenge.get("proof") if isinstance(challenge, dict) else None
        if (
            not isinstance(challenge, dict)
            or challenge.get("type") != "challenge"
            or challenge.get("version") != IPC_PROTOCOL_VERSION
            or not isinstance(server_nonce, str)
            or not re.fullmatch(r"[0-9a-f]{64}", server_nonce)
            or not isinstance(proof, str)
        ):
            raise PermissionError("IPC server authentication failed")

        # Read the secret only after receiving a well-formed challenge. A fake
        # server receives neither the token nor a client proof unless it first
        # proves possession of that token for this fresh client nonce.
        token = self._auth_token_reader()
        expected_server_proof = _handshake_mac(
            token, "server", client_nonce, server_nonce,
        )
        if not hmac.compare_digest(proof, expected_server_proof):
            raise PermissionError("IPC server authentication failed")

        session_key = _derive_session_key(token, client_nonce, server_nonce)
        await self._write_raw_ipc_frame(writer, {
            "type": "authenticate",
            "proof": _handshake_mac(
                token, "client", client_nonce, server_nonce,
            ),
        })
        raw = await asyncio.wait_for(reader.readline(), timeout=10)
        authenticated = json.loads(raw.decode("utf-8").strip()) if raw else {}
        if (
            not _verify_session_frame(
                session_key, "server", 1, authenticated,
            )
            or authenticated.get("type") != "authenticated"
            or authenticated.get("version") != IPC_PROTOCOL_VERSION
        ):
            raise PermissionError("IPC session authentication failed")
        session = {
            "key": session_key, "send_seq": 0, "recv_seq": 2,
            "send_lock": asyncio.Lock(),
        }
        self._ipc_sessions[id(writer)] = session
        return session

    async def _write_authenticated_ipc_frame(self, writer, message):
        """Send a replay-protected frame only after mutual authentication."""
        session = self._ipc_sessions.get(id(writer))
        if session is None:
            raise PermissionError("IPC session is not authenticated")
        async with session["send_lock"]:
            sequence = session["send_seq"] + 1
            session["send_seq"] = sequence
            frame = _sign_session_frame(
                session["key"], "client", sequence, message,
            )
            await self._write_raw_ipc_frame(writer, frame)

    async def _read_authenticated_ipc_frame(self, reader, writer):
        session = self._ipc_sessions.get(id(writer))
        if session is None:
            raise PermissionError("IPC session is not authenticated")
        raw = await reader.readline()
        if not raw:
            return None
        frame = json.loads(raw.decode("utf-8").strip())
        expected_sequence = session["recv_seq"]
        if not _verify_session_frame(
            session["key"], "server", expected_sequence, frame,
        ):
            raise PermissionError("IPC server frame authentication failed")
        session["recv_seq"] = expected_sequence + 1
        return {
            key: value for key, value in frame.items()
            if key not in {"seq", "mac"}
        }

    async def _ipc_connect(self):
        """Maintain persistent TCP connection to IPC server."""
        while True:
            w = None
            try:
                t0 = time.time()
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(IPC_HOST, IPC_PORT), timeout=5)
                await self._ipc_handshake(r, w)
                self.ipc_reader, self.ipc_writer = r, w
                self._ipc_connected = True
                # Register for push
                await self._write_authenticated_ipc_frame(w, {"type": "register"})
                print(f"[IPC] connected to main agent (in {time.time()-t0:.1f}s)")
                # Read loop: dispatch responses or pushes
                while True:
                    try:
                        msg = await asyncio.wait_for(
                            self._read_authenticated_ipc_frame(r, w), timeout=30,
                        )
                    except asyncio.TimeoutError:
                        await self._write_authenticated_ipc_frame(w, {"type": "ping"})
                        continue
                    if msg is None:
                        break
                    if msg.get("type") == "push":
                        asyncio.create_task(self._handle_push(msg))
                    elif msg.get("id") and msg["id"] in _pending_ipc:
                        fut = _pending_ipc.pop(msg["id"])
                        if not fut.done():
                            fut.set_result(msg)
            except (ConnectionRefusedError, OSError):
                self._ipc_connected = False
                print("[IPC] main agent not available, retrying in 10s...")
            except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as e:
                self._ipc_connected = False
                print(f"[IPC] connection error: {e}")
            finally:
                self._ipc_connected = False
                self.ipc_reader = self.ipc_writer = None
                if w is not None:
                    self._ipc_sessions.pop(id(w), None)
                    w.close()
                    try:
                        await w.wait_closed()
                    except (ConnectionResetError, BrokenPipeError):
                        pass
            await asyncio.sleep(10)

    async def _handle_push(self, msg):
        """Handle unsolicited push from main agent and ack actual QQ send result."""
        chat_id = msg.get("chat_id", "")
        text = msg.get("text", "")
        is_group = msg.get("is_group", False)
        push_id = msg.get("push_id", "")
        ok = False
        error = ""
        if chat_id and text:
            print(
                f"[IPC] push chat={_privacy_token(chat_id)} id={push_id} "
                f"chars={len(text)}",
                flush=True,
            )
            try:
                ok = await self.send_text(chat_id, text, is_group=is_group)
                if not ok:
                    error = "send_text returned false"
            except Exception as e:
                error = str(e)
                print(f"[IPC] push send exception id={push_id}: {e}")
        else:
            error = "missing chat_id or text"
            print(
                f"[IPC] push send result id={push_id} "
                f"chat={_privacy_token(chat_id)} ok={ok} "
                f"error_type={type(error).__name__ if error else 'none'}",
                flush=True,
            )
        await self._send_push_ack(push_id, ok, error)

    async def _send_push_ack(self, push_id, ok, error=""):
        if not push_id or not self.ipc_writer:
            return
        try:
            await self._write_authenticated_ipc_frame(self.ipc_writer, {
                "type": "push_ack",
                "push_id": push_id,
                "ok": bool(ok),
                "error": str(error or "")[:300],
            })
            print(f"[IPC] push ack sent id={push_id} ok={ok}", flush=True)
        except Exception as e:
            print(f"[IPC] push ack send failed {push_id}: {e}")


    async def _call_action(self, action, params, timeout=15):
        if not self.ws or self.ws.closed:
            print('[NapCat] WS not connected, cannot call action')
            return None
        self._echo_counter += 1
        echo = f'call_{self._echo_counter}_{int(time.time()*1000)}'
        fut = asyncio.get_event_loop().create_future()
        _pending_echo[echo] = fut
        try:
            await self.ws.send_json({'action': action, 'params': params, 'echo': echo})
            resp = await asyncio.wait_for(fut, timeout=timeout)
            return resp
        except asyncio.TimeoutError:
            _pending_echo.pop(echo, None)
            print(f'[NapCat] action {action} timeout')
            return None
        except Exception as e:
            _pending_echo.pop(echo, None)
            print(f'[NapCat] action {action} error: {e}')
            return None

    async def _send_image(self, chat_id, img_path, *, is_group=False, is_private=False, caption=''):
        if not os.path.isfile(img_path):
            print(f'[NapCat] image not found: {os.path.basename(img_path)}')
            return False
        try:
            with open(img_path, 'rb') as f:
                raw = f.read()
            safe = _evade_censor(raw)
            b64 = base64.b64encode(safe).decode()
            msg = f'[CQ:image,file=base64://{b64}]'
            if caption:
                msg = f'{caption}\n{msg}'
            params = {'message': msg}
            if is_group:
                params['group_id'] = int(chat_id)
                params['message_type'] = 'group'
            else:
                params['user_id'] = int(chat_id)
                params['message_type'] = 'private'
            reply = await self._call_action('send_msg', params)
            ok = reply and reply.get('status') == 'ok'
            if not ok:
                print(f'[NapCat] image send failed status={(reply or {}).get("status", "none")}')
            return ok
        except Exception as e:
            print(f'[NapCat] _send_image error: {e}')
            traceback.print_exc()
            return False

    async def _make_forward_nodes(self, image_paths):
        nodes = []
        fake_names = ['资讯推送', '热图精选', '漫画推荐', '日常分享']
        fake_uins = ['10001', '10002', '10003', '10004']
        for i, img_path in enumerate(image_paths):
            if not os.path.isfile(img_path):
                continue
            try:
                with open(img_path, 'rb') as f:
                    raw = f.read()
                compressed = _compress_for_forward(raw)  # shrink to ~100KB for forward
                safe = _evade_censor(compressed)
                b64 = base64.b64encode(safe).decode()
                idx = i % len(fake_names)
                nodes.append({
                    "type": "node",
                    "data": {
                        "name": fake_names[idx],
                        "uin": fake_uins[idx],
                        "content": f'[CQ:image,file=base64://{b64}]'
                    }
                })
            except Exception as e:
                print(f'[NapCat] forward node error: {e}')
        return nodes

    async def _send_images_as_forward(self, chat_id, image_paths, *, is_group=False):
        if not image_paths:
            return 0
        nodes = await self._make_forward_nodes(image_paths)
        if not nodes:
            return 0
        params = {"messages": nodes}
        if is_group:
            params["group_id"] = int(chat_id)
            action = "send_group_forward_msg"
        else:
            params["user_id"] = int(chat_id)
            action = "send_private_forward_msg"
        reply = await self._call_action(action, params, timeout=120)
        ok = reply and reply.get('status') == 'ok'
        if ok:
            print(f'[NapCat] forward sent: {len(nodes)} images')
            return len(nodes)
        else:
            print('[NapCat] forward failed, fallback to individual')
            return 0

    async def _send_images(self, chat_id, paths, *, is_group=False, is_private=False, batch_size=5, delay=0.5):
        if is_group and len(paths) >= 2:
            sent = await self._send_images_as_forward(chat_id, paths, is_group=True)
            if sent > 0:
                return sent
        sent = 0
        n = len(paths)
        for i, p in enumerate(paths):
            try:
                ok = await self._send_image(chat_id, p, is_group=is_group, is_private=is_private)
                if ok:
                    sent += 1
                if (i + 1) % batch_size == 0 and i + 1 < n:
                    await asyncio.sleep(delay)
            except Exception as e:
                print(f'[NapCat] _send_images error at {i}: {e}')
        return sent

    async def send_text(self, chat_id, content, *, msg_id=None, is_group=False, user_id=None):
        # user_id is operator context used by commands such as /mode.  Delivery
        # still targets chat_id, but accepting it here keeps command context
        # forwarding from turning otherwise valid slash commands into errors.
        await _wait_for_rate_limit()
        file_refs = extract_files(content)
        image_refs = [f for f in file_refs if _is_image_file(f)]
        all_ok = True
        if image_refs:
            print(f'[NapCat] sending {len(image_refs)} images from [FILE] refs')
            sent_images = await self._send_images(chat_id, image_refs[:min(len(image_refs), _IMAGE_MAX_BATCH)], is_group=is_group)
            all_ok = all_ok and sent_images > 0
            for f in image_refs:
                content = content.replace(f'[FILE:{f}]', '')
            content = content.strip()

        parts = split_text(content, self.split_limit) if content.strip() else []
        if not parts and not image_refs:
            parts = ["..."]

        for part in parts:
            params = {'message': part}
            if is_group:
                params['group_id'] = int(chat_id)
                params['message_type'] = 'group'
            else:
                params['user_id'] = int(chat_id)
                params['message_type'] = 'private'
            reply = await self._call_action('send_msg', params)
            if not reply or reply.get('status') != 'ok':
                all_ok = False
                print(f'[NapCat] send failed status={(reply or {}).get("status", "none")}')
        return all_ok

    async def send_done(self, chat_id, raw_text, **ctx):
        is_group = ctx.get('is_group', False)
        file_refs = extract_files(raw_text)
        image_refs = [f for f in file_refs if _is_image_file(f)]
        if image_refs:
            image_refs = image_refs[:_IMAGE_MAX_BATCH]  # limit to avoid spam
            print(f'[NapCat] send_done: sending {len(image_refs)} images')
            asyncio.create_task(self._send_images(chat_id, image_refs, is_group=is_group))
            for f in image_refs:
                raw_text = raw_text.replace(f'[FILE:{f}]', '')
        raw_text = format_for_chat(raw_text, is_group=is_group)
        if raw_text:
            await self.send_text(chat_id, raw_text or '...', **ctx)

    async def _build_health_report(self):
        def run_status():
            try:
                p = subprocess.run(
                    ["bash", HEALTH_SCRIPT],
                    cwd=PROJECT_ROOT,
                    text=True,
                    capture_output=True,
                    timeout=HEALTH_TIMEOUT,
                )
                raw = p.stdout or ""
                if p.stderr:
                    raw = (raw + "\n" + p.stderr).strip()
                return p.returncode, raw.strip()
            except Exception as e:
                return 999, f"{type(e).__name__}: {e}"

        code, raw = await asyncio.to_thread(run_status)
        report = _compact_health_report(raw)
        if code == 0:
            return report
        return f"健康检查脚本异常 exit={code}\n{report}"

    async def _run_assistantctl(self, args, timeout=ASSISTANTCTL_TIMEOUT):
        def run_cmd():
            try:
                p = subprocess.run(
                    [sys.executable, ASSISTANTCTL_SCRIPT, *args],
                    cwd=PROJECT_ROOT,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )
                raw = (p.stdout or "")
                if p.stderr:
                    raw = (raw + "\n" + p.stderr).strip()
                return p.returncode, raw.strip()
            except Exception as e:
                return 999, f"{type(e).__name__}: {e}"

        return await asyncio.to_thread(run_cmd)

    async def _submit_inbox_task(self, body):
        task = (body or "").strip()
        if not task:
            return "用法: /task <要交给远程助手的任务>\n/tasknow <任务> 提交并立即尝试启动\n/tasks 查看队列\n/run 触发队列扫描\n/result 查看最近结果"
        if len(task) > TASK_TEXT_MAX:
            return f"任务太长了，当前 {len(task)} 字；请控制在 {TASK_TEXT_MAX} 字以内，或改用文件投递。"

        code, raw = await self._run_assistantctl(["submit", task])
        if code != 0:
            return f"远程任务提交失败 exit={code}\n{_compact_operator_output(raw)}"

        queued = _compact_operator_output(raw, max_chars=500)
        return (
            "已加入远程助手队列。\n"
            f"{queued}\n"
            "用 /run 触发处理，用 /tasks 查看队列，用 /result 查看最近完成结果。"
        )

    async def _submit_and_run_inbox_task(self, body):
        task = (body or "").strip()
        if not task:
            return "用法: /tasknow <要立即交给远程助手处理的任务>"
        if len(task) > TASK_TEXT_MAX:
            return f"任务太长了，当前 {len(task)} 字；请控制在 {TASK_TEXT_MAX} 字以内，或改用 /task 排队后从服务器文件投递。"

        code, raw = await self._run_assistantctl(["submit", "--now", task], timeout=ASSISTANTCTL_RUN_TIMEOUT)
        report = _compact_operator_output(raw)
        if code == 0:
            return "已提交并触发远程助手处理。\n" + report + "\n用 /tasks 看运行状态，用 /result 看最近结果。"
        return f"提交并启动失败 exit={code}\n{report}"

    async def _run_inbox_once(self):
        code, raw = await self._run_assistantctl(["run-inbox"], timeout=ASSISTANTCTL_RUN_TIMEOUT)
        report = _compact_operator_output(raw)
        if code == 0:
            if report == "(无输出)":
                report = "没有待处理任务，或已有任务正在运行。"
            return "已触发远程助手队列扫描。\n" + report + "\n用 /tasks 看队列，用 /result 看最近结果。"
        return f"触发队列扫描失败 exit={code}\n{report}"

    async def _build_tasks_report(self):
        code, raw = await self._run_assistantctl(["pending"])
        report = _compact_operator_output(raw)
        if code == 0:
            return "远程助手队列\n" + report
        return f"队列查询失败 exit={code}\n{report}"

    async def _build_latest_result_report(self):
        code, raw = await self._run_assistantctl(["results", "--latest", "--tail", "80"], timeout=ASSISTANTCTL_TIMEOUT)
        report = _compact_result_report(raw)
        if code == 0:
            return "最近远程任务结果\n" + report
        return f"最近结果查询失败 exit={code}\n{report}"

    async def _build_alerts_report(self):
        code, raw = await self._run_assistantctl(["alerts", "--verbose", "--limit", "10"])
        report = _compact_operator_output(raw)
        if code == 0:
            return "远程助手告警\n" + report
        return f"告警查询失败 exit={code}\n{report}"

    async def _build_doctor_report(self):
        code, raw = await self._run_assistantctl(["doctor"])
        report = _compact_operator_output(raw, max_chars=1400)
        if code == 0:
            return "远程助手诊断\n" + report
        return f"诊断查询失败 exit={code}\n{report}"

    async def _build_dashboard_report(self):
        code, raw = await self._run_assistantctl(["dashboard", "--tail", "80"])
        report = _compact_operator_output(raw, max_chars=1800)
        if code == 0:
            return "远程助手驾驶舱\n" + report
        return f"驾驶舱查询失败 exit={code}\n{report}"

    async def _build_latest_audit_report(self):
        code, raw = await self._run_assistantctl(["audits", "--latest", "--tail", "120"])
        report = _compact_operator_output(raw, max_chars=1800)
        if code == 0:
            return "最近远程任务审计\n" + report
        return f"审计查询失败 exit={code}\n{report}"

    async def _build_audits_list_report(self):
        code, raw = await self._run_assistantctl(["audits", "--limit", "8"])
        report = _compact_operator_output(raw, max_chars=1600)
        if code == 0:
            return "远程任务审计列表\n" + report
        return f"审计列表查询失败 exit={code}\n{report}"

    async def _build_logs_report(self, arg_text, *, is_group=False):
        if is_group:
            return "日志可能包含聊天内容或内部执行片段；请私聊使用 /logs <inbox|watchdog|napcat> [行数]。"

        parts = (arg_text or "").split()
        source = parts[0].lower() if parts else "inbox"
        if source not in LOG_SOURCES:
            return "用法: /logs <inbox|watchdog|napcat> [行数]\n示例: /logs inbox 40"

        lines = LOG_LINES_DEFAULT
        if len(parts) >= 2:
            try:
                lines = int(parts[1])
            except ValueError:
                return "日志行数需要是数字，例如: /logs inbox 40"
        lines = max(1, min(lines, LOG_LINES_MAX))

        code, raw = await self._run_assistantctl(["logs", source, "--lines", str(lines)])
        report = _compact_operator_output(raw, max_chars=1800)
        if code == 0:
            return f"{source} 日志最近 {lines} 行\n{report}"
        return f"日志查询失败 exit={code}\n{report}"

    def _progress_text(self, elapsed, last_chunk_preview=""):
        if last_chunk_preview:
            return f"\u8fd8\u5728\u5904\u7406\uff0c\u540e\u53f0\u6709\u8fdb\u5c55\uff08{int(elapsed)}s\uff09\u3002\u5b8c\u6210\u540e\u6211\u4f1a\u76f4\u63a5\u53d1\u7ed3\u679c\u3002"
        return f"\u8fd8\u5728\u5904\u7406\uff08{int(elapsed)}s\uff09\uff0c\u6ca1\u6709\u5361\u4f4f\uff0c\u5b8c\u6210\u540e\u6211\u4f1a\u76f4\u63a5\u53d1\u7ed3\u679c\u3002"

    async def run_agent(self, chat_id, text, **ctx):
        """Send message to main agent via IPC TCP (instead of local subagent)."""
        import asyncio, json, uuid, time
        await self.send_text(chat_id, "思考中...", **ctx)
        req_id = uuid.uuid4().hex[:8]
        payload = {
            "type": "req", "id": req_id, "content": text,
            "platform": "qq", "account_id": str(self.self_id or "default"),
            "conversation_id": str(chat_id), "chat_id": str(chat_id),
            "actor_id": str(ctx.get("user_id") or ("" if ctx.get("is_group", False) else chat_id)),
            "is_group": ctx.get("is_group", False),
        }

        started_at = time.time()
        last_visible_at = started_at
        last_activity_at = started_at
        visible_count = 0
        full_text = ""
        last_chunk_preview = ""
        was_queued = False
        writer = None

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(IPC_HOST, IPC_PORT), timeout=5)
            await self._ipc_handshake(reader, writer)
            await self._write_authenticated_ipc_frame(writer, payload)

            while True:
                try:
                    msg = await asyncio.wait_for(
                        self._read_authenticated_ipc_frame(reader, writer),
                        timeout=IPC_READ_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    now = time.time()
                    idle_for = now - last_activity_at
                    if idle_for >= IPC_IDLE_TIMEOUT or now - started_at >= IPC_TOTAL_TIMEOUT:
                        raise asyncio.TimeoutError("IPC response deadline exceeded")
                    first_due = visible_count == 0 and now - started_at >= VISIBLE_PROGRESS_FIRST_SEC
                    repeat_due = visible_count > 0 and now - last_visible_at >= VISIBLE_PROGRESS_REPEAT_SEC
                    if first_due or repeat_due:
                        visible_count += 1
                        last_visible_at = now
                        await self.send_text(
                            chat_id,
                            self._progress_text(now - started_at, last_chunk_preview),
                            **ctx,
                        )
                    continue

                if msg is None:
                    break  # connection closed

                if msg.get("id") != req_id:
                    continue
                last_activity_at = time.time()

                if msg.get("type") == "accepted":
                    was_queued = msg.get("state") == "queued"
                    if was_queued:
                        position = max(1, int(msg.get("queue_position", 1)))
                        await self.send_text(chat_id, f"已排队，前面还有 {position} 个任务。", **ctx)
                elif msg.get("type") == "started":
                    if was_queued:
                        await self.send_text(chat_id, "已开始执行。", **ctx)
                elif msg.get("type") == "chunk":
                    if msg.get("text"):
                        chunk_text = msg["text"]
                        full_text += chunk_text
                        cleaned_preview = _extract_final_answer(chunk_text).replace("\n", " ").strip()
                        if cleaned_preview:
                            last_chunk_preview = cleaned_preview[:80]
                    # Also check visible progress between chunks
                    now = time.time()
                    first_due = visible_count == 0 and now - started_at >= VISIBLE_PROGRESS_FIRST_SEC
                    repeat_due = visible_count > 0 and now - last_visible_at >= VISIBLE_PROGRESS_REPEAT_SEC
                    if first_due or repeat_due:
                        visible_count += 1
                        last_visible_at = now
                        await self.send_text(
                            chat_id,
                            self._progress_text(now - started_at, last_chunk_preview),
                            **ctx,
                        )

                elif msg.get("type") in {"done", "completed", "needs_input", "max_turns", "failed", "stopped"}:
                    terminal_type = msg.get("type")
                    if terminal_type == "needs_input":
                        question = msg.get("question") or "请确认是否继续。"
                        candidates = [str(value) for value in msg.get("candidates") or []]
                        suffix = "\n可选：" + " / ".join(candidates) if candidates else ""
                        await self.send_text(chat_id, f"需要你的明确确认：{question}{suffix}", **ctx)
                        break
                    err = msg.get("error")
                    if err:
                        await self.send_text(chat_id, f"❌ {err}", **ctx)
                    else:
                        text = msg.get("text", full_text)
                        if text:
                            await self.send_done(chat_id, text, **ctx)
                        else:
                            await self.send_text(chat_id, "❌ 任务结束，但没有可见结果。", **ctx)
                    break

        except ConnectionRefusedError:
            await self.send_text(chat_id, "❌ 主 agent (genericagent) 未运行，无法处理请求", **ctx)
        except asyncio.TimeoutError:
            await self.send_text(chat_id, "⏱️ 主 agent 未响应（连接超时）", **ctx)
        except Exception as e:
            import traceback
            traceback.print_exc()
            await self.send_text(chat_id, f"❌ IPC 通信错误: {e}", **ctx)
        finally:
            if writer is not None:
                self._ipc_sessions.pop(id(writer), None)
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError):
                    pass

    async def handle_command(self, chat_id, cmd, **ctx):
        """Override: route all commands through IPC except /help and /subs."""
        op = (cmd or "").split()[0].lower() if cmd.strip() else ""
        if op == "/help":
            return await self.send_text(chat_id, (
                "/help - 显示帮助\n/status - 查看主 agent 状态\n/health - 服务器健康快照\n/stop - 停止当前任务\n"
                "/llm <n> - 切换模型\n/continue <n> - 恢复对话\n"
                "/new - 清空当前上下文\n/subs - 查看子代理状态\n"
                "/btw <msg> - 插问进展\n"
                "/task <任务> - 投递远程助手队列\n/tasknow <任务> - 投递并立即启动\n"
                "/run - 触发队列扫描\n/tasks - 查看队列\n/result - 最近结果\n"
                "/doctor - 诊断和下一步建议\n/dashboard - 助手驾驶舱\n/audit - 最近审计\n/audits - 审计列表\n"
                "/alerts - 查看告警\n/logs <源> [行数] - 私聊查看日志"
            ), **ctx)
        if op == "/subs":
            from frontends.shared.sub_agent import list_subagents
            subs = list_subagents()
            if not subs:
                return await self.send_text(chat_id, "当前没有活跃的子代理", **ctx)
            lines = ["📋 子代理状态:"]
            for s in subs:
                icon = "🟢" if s["alive"] else "🔴"
                task = s["task"][:60]
                progress = s["progress"] or ""
                progress_str = f" — {progress[:50]}" if progress and s["alive"] else ""
                turns = s.get("turns", 0)
                time_str = ""
                if s.get("created_at"):
                    time_str = f" [{s['created_at'][5:16]}]"
                lines.append(f"  {icon} [{s['id'][:20]}]{time_str} {task}{progress_str} ({turns}轮)")
            return await self.send_text(chat_id, "\n".join(lines), **ctx)
        if op == "/status":
            from frontends.shared.sub_agent import list_subagents
            subs = list_subagents()
            alive = sum(1 for s in subs if s.get("alive"))
            ipc = "connected" if self._ipc_connected else "disconnected"
            return await self.send_text(chat_id, (
                f"主 Agent IPC: {ipc}\n"
                f"子代理: {alive} active / {len(subs)} total\n"
                f"待处理 IPC replies: {len(_pending_ipc)}"
            ), **ctx)
        if op in ("/health", "/健康", "/体检"):
            return await self.send_text(chat_id, await self._build_health_report(), **ctx)
        if op in ("/task", "/submit", "/todo"):
            body = cmd.split(maxsplit=1)[1] if len(cmd.split(maxsplit=1)) > 1 else ""
            return await self.send_text(chat_id, await self._submit_inbox_task(body), **ctx)
        if op in ("/tasknow", "/now"):
            body = cmd.split(maxsplit=1)[1] if len(cmd.split(maxsplit=1)) > 1 else ""
            return await self.send_text(chat_id, await self._submit_and_run_inbox_task(body), **ctx)
        if op in ("/run", "/runinbox", "/执行队列"):
            return await self.send_text(chat_id, await self._run_inbox_once(), **ctx)
        if op in ("/tasks", "/pending", "/queue", "/队列"):
            return await self.send_text(chat_id, await self._build_tasks_report(), **ctx)
        if op in ("/result", "/results", "/结果"):
            return await self.send_text(chat_id, await self._build_latest_result_report(), **ctx)
        if op in ("/alerts", "/alert", "/告警"):
            return await self.send_text(chat_id, await self._build_alerts_report(), **ctx)
        if op in ("/doctor", "/next", "/建议"):
            return await self.send_text(chat_id, await self._build_doctor_report(), **ctx)
        if op in ("/dashboard", "/dash", "/看板", "/驾驶舱"):
            return await self.send_text(chat_id, await self._build_dashboard_report(), **ctx)
        if op in ("/audit", "/审计"):
            return await self.send_text(chat_id, await self._build_latest_audit_report(), **ctx)
        if op in ("/audits", "/审计列表"):
            return await self.send_text(chat_id, await self._build_audits_list_report(), **ctx)
        if op in ("/logs", "/log", "/日志"):
            body = cmd.split(maxsplit=1)[1] if len(cmd.split(maxsplit=1)) > 1 else ""
            return await self.send_text(chat_id, await self._build_logs_report(body, is_group=ctx.get("is_group", False)), **ctx)
        if op == "/btw":
            from frontends.shared.sub_agent import list_subagents
            body = cmd[len("/btw"):].strip()
            subs = list_subagents()
            active = [s for s in subs if s.get("alive")]
            if not active:
                msg = "当前没有活跃子代理。"
            else:
                lines = ["当前子代理进展:"]
                for s in active[:5]:
                    task = s.get("task", "")[:60]
                    progress = s.get("progress", "")[:100]
                    turns = s.get("turns", 0)
                    lines.append(f"- {s.get('id', '')[:20]} | {turns}轮 | {task} | {progress}")
                msg = "\n".join(lines)
            if body:
                msg += f"\n\n你的插问: {body}"
            return await self.send_text(chat_id, msg, **ctx)
        if op == "/mode":
            parts = cmd.split()
            try:
                from memory.qq_mode_manager import get_mode, set_mode
            except Exception as e:
                return await self.send_text(chat_id, f"模式系统不可用: {e}", **ctx)
            if len(parts) == 1:
                return await self.send_text(chat_id, f"当前模式: {get_mode(chat_id)}", **ctx)
            mode = parts[1].lower()
            operator = str(ctx.get("user_id", ""))
            result = set_mode(chat_id, mode, operator)
            return await self.send_text(chat_id, result.get("message", str(result)), **ctx)
        # All other commands → IPC
        await self.run_agent(chat_id, cmd, **ctx)

    def _keyword_action_to_prompt(self, action, content):
        if action == "health_check":
            return "/health"
        if action == "system_status":
            return "/status"
        if action == "progress_status":
            return "/btw " + content
        if action == "daily_report":
            return "请生成今日日报，并在完成后通过QQ主动汇报摘要。"
        return content

    async def on_message(self, data, is_group=False):
        try:
            msg_id = str(data.get('message_id', ''))
            if msg_id in PROCESSED_IDS:
                return
            PROCESSED_IDS.append(msg_id)

            content = (data.get('raw_message') or data.get('message') or '').strip()
            if not content:
                return

            sender = data.get('sender', {})
            user_id = str(sender.get('user_id', 'unknown'))
            if not public_access(ALLOWED) and user_id not in ALLOWED:
                print('[NapCat] blocked unauthorized message')
                return
            chat_id = str(data.get('group_id', '') or user_id) if is_group else user_id
            nickname = sender.get('nickname', user_id)
            gid = data.get('group_id', 'N/A')

            if is_group:
                mentioned = bool(self.self_id and f'[CQ:at,qq={self.self_id}]' in content)
                content = _RE_CQ.sub('', content).strip()
                if not content:
                    return
                if not mentioned:
                    try:
                        from memory.qq_mode_manager import get_mode
                        from memory.qq_keyword_engine import match_keyword
                        mode = get_mode(chat_id)
                        matches = match_keyword(content) if mode == "active" else []
                    except Exception as e:
                        print(f"[NapCat] active mode check failed: {e}")
                        matches = []
                    if not matches:
                        return
                    action = matches[0].get("action", "")
                    content = self._keyword_action_to_prompt(action, content)
                    print(_safe_message_event(
                        'group keyword', user_id=user_id, chat_id=chat_id,
                        content=content, action=action,
                    ))
                else:
                    print(_safe_message_event(
                        'group mention', user_id=user_id, chat_id=chat_id,
                        content=content,
                    ))
            else:
                print(_safe_message_event(
                    'private message', user_id=user_id, chat_id=chat_id,
                    content=content,
                ))

            if content.startswith('/'):
                await self.handle_command(chat_id, content, msg_id=msg_id, is_group=is_group, user_id=user_id)
                return

            asyncio.create_task(self.run_agent(chat_id, content, msg_id=msg_id, is_group=is_group, user_id=user_id))
            gc.collect()
        except Exception:
            traceback.print_exc()

    async def _ws_loop(self):
        delay, max_delay = 5, 300
        gc_interval = 60
        last_gc = time.time()
        while True:
            try:
                print(f'[NapCat] WebSocket connecting to {WS_URL}...')
                async with aiohttp.ClientSession() as sess:
                    self.ws_session = sess
                    async with sess.ws_connect(WS_URL) as ws:
                        self.ws = ws
                        print('[NapCat] WebSocket connected')
                        delay = 5
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                event = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue
                            if event.get('echo'):
                                _handle_ws_response(event)
                                continue
                            post_type = event.get('post_type', '')
                            if post_type == 'meta_event' and event.get('meta_event_type') == 'lifecycle':
                                self.self_id = event.get('self_id')
                                print(f'[NapCat] bot online: {_privacy_token(self.self_id)}')
                            if post_type == 'message':
                                mt = event.get('message_type', '')
                                if mt == 'private':
                                    await self.on_message(event, is_group=False)
                                elif mt == 'group':
                                    await self.on_message(event, is_group=True)
                            elif post_type == 'meta_event' and event.get('meta_event_type') != 'heartbeat':
                                print(f'[NapCat] meta: {event.get("meta_event_type")}')
            except Exception as e:
                print(f'[NapCat] WS error: {e}')
            print(f'[NapCat] reconnect in {delay}s...')
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
            if time.time() - last_gc > gc_interval:
                gc.collect()
                last_gc = time.time()

    async def _heartbeat_loop(self):
        while True:
            try:
                await asyncio.to_thread(write_heartbeat)
            except Exception as exc:
                print(f'[NapCat] heartbeat write failed: {type(exc).__name__}')
            await asyncio.sleep(2)

    async def start(self):
        print("[IPC] connecting to main agent...")
        await asyncio.gather(
            self._ws_loop(), self._ipc_connect(), self._heartbeat_loop()
        )

if __name__ == '__main__':
    _LOCK_SOCK = ensure_single_instance(19529, 'NapCatQQ')
    redirect_log(__file__, 'napcat_qqapp.log', 'NapCatQQ', ALLOWED)
    asyncio.run(NapCatApp().start())
