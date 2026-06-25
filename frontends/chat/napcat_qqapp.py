import gc
import asyncio, base64, io, json, os, random, re, sys, threading, time, traceback
from collections import deque

import aiohttp
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from frontends.shared.chatapp_common import (AgentChatMixin, ensure_single_instance,
    public_access, redirect_log, split_text, extract_files,
    _extract_final_answer)
from llmcore import mykeys

WS_URL   = os.environ.get('NAPCAT_WS', 'ws://127.0.0.1:3001/ws')
ALLOWED  = {str(x).strip() for x in mykeys.get('qq_allowed_users', []) if str(x).strip()}

IPC_HOST = "127.0.0.1"
IPC_PORT = 9001
_RE_CQ = re.compile(r'\[CQ:[^\]]+\]')

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}
_IMAGE_MAX_BATCH = 5  # max images per single reply

# ── Rate limiter ──
_RATE_LIMIT = deque(maxlen=20)  # track last 20 send times
_RATE_WINDOW = 5  # seconds
_RATE_MAX = 8  # max sends per window


def _check_rate_limit():
    """Returns True if we should delay sending."""
    now = time.time()
    while _RATE_LIMIT and now - _RATE_LIMIT[0] > _RATE_WINDOW:
        _RATE_LIMIT.popleft()
    if len(_RATE_LIMIT) >= _RATE_MAX:
        delay = _RATE_LIMIT[0] + _RATE_WINDOW - now
        if delay > 0:
            time.sleep(delay)
    _RATE_LIMIT.append(now)


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

class NapCatApp(AgentChatMixin):
    label, source, split_limit = 'NapCatQQ', 'napcatqq', 1500

    def __init__(self):
        super().__init__(_dummy_agent, USER_TASKS)
        self.ws = None
        self.ws_session = None
        self._echo_counter = 0
        self.self_id = None
        self.ipc_reader = None
        self.ipc_writer = None
        self._ipc_connected = False

    async def _ipc_connect(self):
        """Maintain persistent TCP connection to IPC server."""
        while True:
            try:
                t0 = time.time()
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(IPC_HOST, IPC_PORT), timeout=5)
                self.ipc_reader, self.ipc_writer = r, w
                self._ipc_connected = True
                # Register for push
                reg = json.dumps({"type": "register"}) + "\n"
                w.write(reg.encode("utf-8"))
                await w.drain()
                print(f"[IPC] connected to main agent (in {time.time()-t0:.1f}s)")
                # Read loop: dispatch responses or pushes
                while True:
                    line = await asyncio.wait_for(r.readline(), timeout=600)
                    if not line:
                        break
                    msg = json.loads(line.decode("utf-8").strip())
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
            await asyncio.sleep(10)

    async def _handle_push(self, msg):
        """Handle unsolicited push from main agent (e.g. sub-agent completed)."""
        chat_id = msg.get("chat_id", "")
        text = msg.get("text", "")
        is_group = msg.get("is_group", False)
        if chat_id and text:
            print(f"[IPC] push to {chat_id}: {text[:60]}...")
            await self.send_text(chat_id, text, is_group=is_group)

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
            print(f'[NapCat] image not found: {img_path}')
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
                print(f'[NapCat] image send failed: {reply}')
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
            print(f'[NapCat] forward failed ({reply}), fallback to individual')
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

    async def send_text(self, chat_id, content, *, msg_id=None, is_group=False):
        _check_rate_limit()  # Enforce QQ rate limit
        file_refs = extract_files(content)
        image_refs = [f for f in file_refs if _is_image_file(f)]
        if image_refs:
            print(f'[NapCat] sending {len(image_refs)} images from [FILE] refs')
            await self._send_images(chat_id, image_refs[:min(len(image_refs), _IMAGE_MAX_BATCH)], is_group=is_group)
            for f in image_refs:
                content = content.replace(f'[FILE:{f}]', '')
            content = content.strip()

        MAX_TOTAL = 2000
        if len(content) > MAX_TOTAL:
            content = content[:MAX_TOTAL] + "\n\n... (输出已截断, 完整结果请查看文件)"

        parts = split_text(content, self.split_limit) if content.strip() else []
        for part in parts:
            params = {'message': part}
            if is_group:
                params['group_id'] = int(chat_id)
                params['message_type'] = 'group'
            else:
                params['user_id'] = int(chat_id)
                params['message_type'] = 'private'
            reply = await self._call_action('send_msg', params)
            if reply and reply.get('status') != 'ok':
                print(f'[NapCat] send failed: {reply}')

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
        raw_text = _extract_final_answer(raw_text)
        if raw_text:
            await self.send_text(chat_id, raw_text or '...', **ctx)

    async def run_agent(self, chat_id, text, **ctx):
        """Send message to main agent via IPC TCP (instead of local subagent)."""
        import asyncio, json, uuid, time
        await self.send_text(chat_id, "思考中...", **ctx)
        req_id = uuid.uuid4().hex[:8]
        payload = json.dumps({"type": "req", "id": req_id, "content": text}, ensure_ascii=False) + "\n"

        last_ping = time.time()
        ping_count = 0
        ping_intervals = [30, 45, 60]
        full_text = ""

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(IPC_HOST, IPC_PORT), timeout=5)
            writer.write(payload.encode("utf-8"))
            await writer.drain()

            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=10)
                except asyncio.TimeoutError:
                    # 10s no data — send ping if due
                    interval = ping_intervals[ping_count] if ping_count < len(ping_intervals) else ping_intervals[-1]
                    if time.time() - last_ping > interval:
                        ping_count += 1
                        elapsed = int(time.time() - last_ping)
                        await self.send_text(chat_id, f"⏳ 还在处理中（已等待 {elapsed}s）...", **ctx)
                        last_ping = time.time()
                    continue

                if not line:
                    break  # connection closed

                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError:
                    continue

                if msg.get("id") != req_id:
                    continue

                if msg.get("type") == "chunk":
                    last_ping = time.time()  # got data, reset ping timer
                    if msg.get("text"):
                        full_text += msg["text"]

                elif msg.get("type") == "done":
                    err = msg.get("error")
                    if err:
                        await self.send_text(chat_id, f"❌ 错误: {err}", **ctx)
                    else:
                        text = msg.get("text", full_text)
                        if text:
                            await self.send_done(chat_id, text, **ctx)
                        else:
                            await self.send_text(chat_id, "✅ 处理完成", **ctx)
                    break

            writer.close()

        except ConnectionRefusedError:
            await self.send_text(chat_id, "❌ 主 agent (genericagent) 未运行，无法处理请求", **ctx)
        except asyncio.TimeoutError:
            await self.send_text(chat_id, "⏱️ 主 agent 未响应（连接超时）", **ctx)
        except Exception as e:
            import traceback
            traceback.print_exc()
            await self.send_text(chat_id, f"❌ IPC 通信错误: {e}", **ctx)

    async def handle_command(self, chat_id, cmd, **ctx):
        """Override: route all commands through IPC except /help and /subs."""
        op = (cmd or "").split()[0].lower() if cmd.strip() else ""
        if op == "/help":
            return await self.send_text(chat_id, (
                "/help - 显示帮助\n/status - 查看主 agent 状态\n/stop - 停止当前任务\n"
                "/llm <n> - 切换模型\n/continue <n> - 恢复对话\n"
                "/new - 清空当前上下文\n/subs - 查看子代理状态\n"
                "/btw <msg> - 插问进展"
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
                lines.append(f"  {icon} [{s['id'][:20]}]{time_str} {t}{progress_str} ({turns}轮)")
            return await self.send_text(chat_id, "\n".join(lines), **ctx)
        # All other commands → IPC
        await self.run_agent(chat_id, cmd, **ctx)

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
            chat_id = str(data.get('group_id', '') or user_id) if is_group else user_id
            nickname = sender.get('nickname', user_id)
            gid = data.get('group_id', 'N/A')

            if is_group:
                if not self.self_id or f'[CQ:at,qq={self.self_id}]' not in content:
                    return
                content = _RE_CQ.sub('', content).strip()
                if not content:
                    return
                print(f'[NapCat] @bot in group {gid} from {nickname}({user_id}): {content[:80]}')
            else:
                if not public_access(ALLOWED) and user_id not in ALLOWED:
                    print(f'[NapCat] blocked {nickname}({user_id}) private')
                    return
                print(f'[NapCat] private {nickname}({user_id}): {content[:80]}')

            if content.startswith('/'):
                await self.handle_command(chat_id, content, msg_id=msg_id, is_group=is_group)
                return

            asyncio.create_task(self.run_agent(chat_id, content, msg_id=msg_id, is_group=is_group))
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
                                print(f'[NapCat] bot online: {self.self_id}')
                            if post_type == 'message':
                                mt = event.get('message_type', '')
                                if mt == 'private':
                                    await self.on_message(event, is_group=False)
                                elif mt == 'group':
                                    await self.on_message(event, is_group=True)
                            elif post_type == 'meta_event':
                                print(f'[NapCat] meta: {event.get("meta_event_type")}')
            except Exception as e:
                print(f'[NapCat] WS error: {e}')
            print(f'[NapCat] reconnect in {delay}s...')
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
            if time.time() - last_gc > gc_interval:
                gc.collect()
                last_gc = time.time()

    async def start(self):
        print("[IPC] connecting to main agent...")
        await asyncio.gather(self._ws_loop(), self._ipc_connect())

if __name__ == '__main__':
    _LOCK_SOCK = ensure_single_instance(19529, 'NapCatQQ')
    redirect_log(__file__, 'napcat_qqapp.log', 'NapCatQQ', ALLOWED)
    asyncio.run(NapCatApp().start())
