"""IPC TCP server — bridges QQ messages to the main agent process.

NapCat maintains ONE persistent TCP connection for all communication.

Protocol (JSON-line over TCP, 127.0.0.1:9001):
  Request:   {"type":"req","id":"<short_id>","content":"<msg>","chat_id":"<qq_id>","is_group":bool}
  Chunk:     {"type":"chunk","id":"<short_id>","text":"<partial>"}
  Response:  {"type":"done","id":"<short_id>","text":"<final>","error":null|<str>}
  Push:      {"type":"push","push_id":"<id>","chat_id":"<qq_id>","is_group":bool,"text":"<msg>"}
  Push Ack:  {"type":"push_ack","push_id":"<id>","ok":bool,"error":"<err>"}
"""

import asyncio, json, queue, threading, os, glob, time, uuid, traceback, hashlib
from session_store import ConversationIdentity

IPC_HOST = "127.0.0.1"
IPC_PORT = 9001
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SUB_AGENTS_DIR = os.path.join(PROJECT_ROOT, "temp", "sub_agents")
SCHEDULER_DONE_DIR = os.path.join(PROJECT_ROOT, "sche_tasks", "done")
LEGACY_REPORT_PUSH_FILE = os.path.join(PROJECT_ROOT, "temp", "report_push.json")
MONITOR_PUSH_QUEUE_FILE = os.path.join(PROJECT_ROOT, "temp", "monitor_push_queue.jsonl")

# ── Simple semantic cache ──
_CACHE = {}  # hash → {reply, ttl}
_CACHE_MAX = 256
_CACHE_TTL = 300  # 5 minutes
CONFIRM_VALUES = {"允许", "同意", "确认", "继续", "yes", "y"}
REJECT_VALUES = {"拒绝", "不同意", "取消", "停止", "no", "n"}
PENDING_INPUT_TTL = 600


def _cache_key(text, chat_id="", generation=0, route="quick_chat"):
    """Simple cache key: lowercased stripped text, hashed."""
    # Use a very simple "normalize and hash" approach
    t = text.lower().strip()
    # Remove common prefixes that shouldn't affect cache
    t = t.lstrip("?？!！。，,. ")
    raw = f"{chat_id}\0{int(generation)}\0{route}\0{t}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_cache(text, chat_id="", generation=0, route="quick_chat"):
    """Returns cached reply if found and not expired, else None."""
    now = time.time()
    dead = []
    hit = None
    for k, v in list(_CACHE.items()):
        if now > v.get("ttl", 0):
            dead.append(k)
        elif k == _cache_key(text, chat_id, generation, route):
            hit = v["reply"]
    for k in dead:
        _CACHE.pop(k, None)
    return hit


def _set_cache(text, reply, chat_id="", generation=0, route="quick_chat"):
    if len(_CACHE) >= _CACHE_MAX:
        # Remove oldest
        for k in list(_CACHE)[:len(_CACHE) - _CACHE_MAX + 32]:
            _CACHE.pop(k, None)
    _CACHE[_cache_key(text, chat_id, generation, route)] = {
        "reply": reply,
        "ttl": time.time() + _CACHE_TTL,
    }


def schedule_push(text, chat_id="", is_group=False):
    """Write a push token for the scheduler. IPC monitor reads and pushes it.
    Thread-safe: uses file-based signaling."""
    d = SCHEDULER_DONE_DIR
    os.makedirs(d, exist_ok=True)
    fname = f"push_{uuid.uuid4().hex[:8]}.push"
    try:
        with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
            json.dump({"text": text, "chat_id": chat_id, "is_group": is_group}, f)
    except Exception:
        pass


def _normalize_push_token(token):
    """Return (chat_id, is_group, text) for every supported push payload."""
    if not isinstance(token, dict):
        return "", False, ""

    chat_id = str(
        token.get("chat_id")
        or token.get("group_id")
        or token.get("user_id")
        or ""
    ).strip()
    text = str(token.get("text") or token.get("content") or "").strip()

    is_group = token.get("is_group")
    if is_group is None:
        is_group = bool(token.get("group_id"))

    message_type = str(token.get("message_type") or token.get("type") or "push")
    if text and message_type and not text.startswith("["):
        text = f"[{message_type}]\n{text}"

    return chat_id, bool(is_group), text


# ── Push retry helpers ──

def _retry_delay_seconds(attempts):
    attempts = max(1, int(attempts or 1))
    return min(300, 5 * (2 ** min(attempts - 1, 6)))


def _can_attempt_push(meta, now=None):
    now = time.time() if now is None else now
    return float(meta.get("next_retry_at", 0) or 0) <= now


def _mark_push_success(meta_file, meta, push_id, now=None):
    now = int(time.time() if now is None else now)
    meta["pushed"] = True
    meta["push_id"] = push_id
    meta["push_last_ok_at"] = now
    meta["push_last_error"] = ""
    meta.pop("next_retry_at", None)
    _write_meta_file(meta_file, meta)


def _mark_push_failure(meta_file, meta, error, now=None):
    now = int(time.time() if now is None else now)
    attempts = int(meta.get("push_attempts", 0) or 0) + 1
    meta["pushed"] = False
    meta["push_attempts"] = attempts
    meta["push_last_error"] = str(error or "push failed")[:300]
    meta["next_retry_at"] = now + _retry_delay_seconds(attempts)
    _write_meta_file(meta_file, meta)


def _write_meta_file(meta_file, meta):
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


class IpcServer:
    def __init__(self, agent, host=IPC_HOST, port=IPC_PORT, clock=time.time):
        self.agent = agent
        self.host = host
        self.port = port
        self._push_writer = None  # persistent napcat connection for pushes
        self._pending_push_acks = {}
        self._lock = threading.Lock()
        self._loop = None
        self._thread = None
        self.clock = clock
        self.pending_inputs = {}

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ipc-server")
        self._thread.start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        server = await asyncio.start_server(self._handle, self.host, self.port)
        print(f"[IPC] TCP server listening on {self.host}:{self.port}")
        # Start background push monitor
        asyncio.create_task(self._push_monitor())
        async with server:
            await server.serve_forever()

    async def _handle(self, reader, writer):
        """Handle one persistent connection. Read messages in a loop."""
        request_tasks = set()
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break  # connection closed
                msg = json.loads(raw.decode("utf-8").strip())
                if msg.get("type") == "req":
                    task = asyncio.create_task(
                        self._run_request(msg, reader, writer)
                    )
                    request_tasks.add(task)
                    task.add_done_callback(request_tasks.discard)
                elif msg.get("type") == "register":
                    with self._lock:
                        self._push_writer = writer
                    print(f"[IPC] napcat registered for push")
                elif msg.get("type") == "push_ack":
                    await self._handle_push_ack(msg)
                elif msg.get("type") == "ping":
                    pass  # keepalive, no response needed
        except (ConnectionResetError, BrokenPipeError, Exception) as e:
            print(f"[IPC] connection closed: {e}")
        finally:
            for task in request_tasks:
                if not task.done():
                    task.cancel()
            if request_tasks:
                await asyncio.gather(*request_tasks, return_exceptions=True)
            with self._lock:
                # Only clear if generation hasn't changed (newer connection already registered)
                if self._push_writer is writer:
                    self._push_writer = None
                    print("[IPC] napcat push connection lost")
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _run_request(self, msg, reader, writer):
        """Contain request failures so every connected client gets a terminal frame."""
        try:
            await self._process_request(msg, reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            req_id = msg.get("id", "")
            print(f"[IPC] request failed: {type(exc).__name__}")
            try:
                await self._send_json(writer, {
                    "type": "failed", "id": req_id, "text": "",
                    "error": "internal IPC request failure",
                })
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _handle_push_ack(self, msg):
        push_id = str(msg.get("push_id") or "")
        if not push_id:
            return
        with self._lock:
            fut = self._pending_push_acks.pop(push_id, None)
        if fut and not fut.done():
            fut.set_result(msg)

    async def _process_request(self, msg, reader, writer):
        """Process a single request on the persistent connection."""
        req_id = msg.get("id", uuid.uuid4().hex[:8])
        content = msg.get("content", "")
        try:
            identity = ConversationIdentity.from_message(msg)
        except ValueError as error:
            await self._send_json(writer, {"type": "failed", "id": req_id, "text": "", "error": str(error)})
            return
        explicit_identity = any(
            key in msg for key in ("platform", "account_id", "account", "conversation_id")
        )
        legacy_chat_id = str(msg.get("chat_id", "") or "")
        chat_id = identity.key if explicit_identity else legacy_chat_id
        identity_dict = identity.as_dict()
        is_group = msg.get("is_group", False)

        if not content:
            await self._send_json(writer, {"type": "failed", "id": req_id, "text": "", "error": "empty message"})
            return

        command = content.strip().lower()
        if command == "/new":
            reset = getattr(self.agent, "reset_conversation", None)
            generation = (
                reset(chat_id, identity_dict)
                if reset else self.agent.reset_chat_session(chat_id)
            )
            self.pending_inputs.pop(chat_id, None)
            await self._send_json(writer, {
                "type": "completed", "id": req_id,
                "text": f"🆕 已开启新对话（会话版本 {generation}），上下文已清空。",
                "error": "",
            })
            return
        if command.startswith("/history ") or command.startswith("/历史 "):
            query = content.split(" ", 1)[1].strip()
            rows = self.agent.session_store.search(identity, query, limit=5)
            text = "\n".join(
                f"- [{row['role']}] {row['content'][:240]}" for row in rows
            ) or "当前会话没有匹配的历史记录。"
            await self._send_json(writer, {
                "type": "completed", "id": req_id, "text": text, "error": "",
            })
            return
        if command == "/skills":
            items = self.agent.skill_manager.list_pending()
            text = "\n".join(
                f"- {item['id']} {item['slug']} {item['status']}" for item in items
            ) or "没有待审批的 Skill。"
            await self._send_json(writer, {"type": "completed", "id": req_id, "text": text, "error": ""})
            return
        if command.startswith("/skill show "):
            proposal_id = content.split()[-1]
            try:
                meta, skill = self.agent.skill_manager.get(proposal_id)
                text = f"{meta['slug']}\nSHA256: {meta['sha256']}\n原因: {meta.get('reason','')}\n\n{skill[:4000]}"
                error = ""
            except Exception as exc:
                text, error = "", str(exc)
            await self._send_json(writer, {"type": "completed" if not error else "failed", "id": req_id, "text": text, "error": error})
            return
        if command.startswith("/skill approve ") or command.startswith("/skill reject "):
            proposal_id = content.split()[-1]
            try:
                action = "approve" if command.startswith("/skill approve ") else "reject"
                result = getattr(self.agent.skill_manager, action)(proposal_id, identity)
                text, error = json.dumps(result, ensure_ascii=False), ""
            except Exception as exc:
                text, error = "", str(exc)
            await self._send_json(writer, {"type": "completed" if not error else "failed", "id": req_id, "text": text, "error": error})
            return
        if command == "/stop":
            stopped = bool(
                getattr(self.agent, "abort_chat_task", lambda _chat_id: False)(chat_id)
            )
            self.pending_inputs.pop(chat_id, None)
            await self._send_json(writer, {
                "type": "completed", "id": req_id,
                "text": (
                    "⏹️ 已请求停止当前任务。"
                    if stopped else "当前会话没有正在执行的任务。"
                ),
                "error": "",
            })
            return
        if command == "/status":
            status = self.agent.task_status(chat_id)
            await self._send_json(writer, {
                "type": "completed", "id": req_id,
                "text": status["text"], "error": "",
            })
            return

        pending = self.pending_inputs.get(chat_id)
        if pending and self.clock() - pending.get("created_at", 0) > PENDING_INPUT_TTL:
            self.pending_inputs.pop(chat_id, None)
            pending = None
        if pending:
            answer = content.strip().lower()
            if answer in REJECT_VALUES:
                self.pending_inputs.pop(chat_id, None)
                await self._send_json(writer, {
                    "type": "completed", "id": req_id,
                    "text": "已拒绝并取消待确认操作。", "error": "",
                })
                return
            if answer not in CONFIRM_VALUES:
                await self._send_json(writer, {
                    "type": "needs_input", "id": req_id, "text": "", "error": "",
                    "question": pending["question"],
                    "candidates": pending.get("candidates", []),
                })
                return
            self.pending_inputs.pop(chat_id, None)
            content = "用户已明确允许。请继续上一项待确认操作。"

        try:
            from frontends.shared.chat_router import classify_chat_message
            chat_route = classify_chat_message(content)
        except Exception:
            chat_route = "long_task" if len(content) >= 120 else "quick_chat"
        generation = int(getattr(self.agent, "session_generations", {}).get(chat_id, 0))
        if chat_route == "resend_last":
            store = getattr(self.agent, "session_store", None)
            rows = store.recent(identity, generation, limit=20) if store else []
            previous = next(
                (
                    str(row.get("content") or "")
                    for row in reversed(rows)
                    if row.get("role") == "assistant" and str(row.get("content") or "").strip()
                ),
                "",
            )
            if previous:
                await self._send_json(writer, {
                    "type": "completed", "id": req_id, "text": previous, "error": "",
                })
            else:
                await self._send_json(writer, {
                    "type": "failed", "id": req_id, "text": "",
                    "error": "当前会话没有可重发的上一条完整回复。",
                })
            return
        if chat_route == "quick_tool":
            status = self.agent.task_status(chat_id)
            await self._send_json(writer, {
                "type": "completed", "id": req_id,
                "text": status["text"], "error": "",
            })
            return

        # Check semantic cache for short/common queries
        cacheable = (
            chat_route == "quick_chat"
            and len(content) < 100
            and not any(word in content for word in ("完成了吗", "进展", "状态", "为什么失败"))
        )
        if cacheable:
            cached = _check_cache(content, chat_id, generation, chat_route)
            if cached:
                print(f"[IPC] cache hit for: {content[:60]}...")
                await self._send_json(writer, {"type": "completed", "id": req_id, "text": cached, "error": "", "cached": True})
                return

        # Build personality hint based on chat type
        personality = ""
        if chat_id:
            personality = "群聊" if is_group else "私聊"
            if is_group and chat_id not in getattr(self.agent, 'session_histories', {}):
                personality += "首次消息"
            elif chat_id in getattr(self.agent, 'session_configs', {}):
                cfg = self.agent.session_configs.get(chat_id, {})
                if cfg.get("style"):
                    personality += f"\n回复风格: {cfg['style']}"

        dq = queue.Queue()
        from frontends.shared.task_protocol import turns_for_route
        queue_position = self.agent.task_queue.qsize() + (1 if self.agent.is_running else 0)
        queued_task = {
            "query": content,
            "source": "ipc",
            "chat_id": chat_id,
            "images": [],
            "output": dq,
            "max_turns": turns_for_route(chat_route),
            "reset_history": False,  # Session-isolated
            "personality": personality,
            "chat_route": chat_route,
            "generation": generation,
            "request_id": req_id,
            "conversation_identity": identity_dict,
            "queued_at": self.clock(),
        }
        try:
            self.agent.task_queue.put_nowait(queued_task)
        except queue.Full:
            await self._send_json(writer, {
                "type": "failed", "id": req_id, "text": "",
                "error": "任务队列繁忙，请稍后重试。",
            })
            return
        await self._send_json(writer, {
            "type": "accepted",
            "id": req_id,
            "state": "queued" if queue_position else "starting",
            "queue_position": queue_position,
        })

        loop = asyncio.get_running_loop()
        full_text = ""
        terminal = None
        while True:
            item = await loop.run_in_executor(None, dq.get)
            if "started" in item:
                await self._send_json(writer, {
                    "type": "started", "id": req_id,
                    "task": item.get("started") or {},
                })
            if "next" in item:
                chunk = item.get("next", "")
                if chunk:
                    full_text += chunk
                    await self._send_json(writer, {"type": "chunk", "id": req_id, "text": chunk})
            if "done" in item:
                full_text = item.get("done", "")
                terminal = item.get("terminal") or {
                    "state": "completed", "text": full_text, "error": "",
                    "question": "", "candidates": [],
                }
                break

        await self._send_json(writer, {
            "type": terminal["state"],
            "id": req_id,
            "text": terminal.get("text", ""),
            "error": terminal.get("error", ""),
            "question": terminal.get("question", ""),
            "candidates": terminal.get("candidates", []),
        })

        # Cache short replies (not error, not empty)
        if terminal["state"] == "needs_input":
            self.pending_inputs[chat_id] = {
                "question": terminal.get("question") or "请确认是否继续。",
                "candidates": terminal.get("candidates", []),
                "created_at": self.clock(),
            }
        if terminal["state"] == "completed" and cacheable and full_text and len(full_text) > 5:
            _set_cache(content, full_text, chat_id, generation, chat_route)

        # Register newly created sub-agents for push notification
        self._register_recent_subagents(chat_id, is_group)

    def _register_recent_subagents(self, chat_id, is_group):
        """Write .ipc_meta.json for sub-agent dirs created in the last 30s."""
        if not os.path.isdir(SUB_AGENTS_DIR):
            return
        now = time.time()
        for name in os.listdir(SUB_AGENTS_DIR):
            d = os.path.join(SUB_AGENTS_DIR, name)
            meta = os.path.join(d, ".ipc_meta.json")
            if os.path.isdir(d) and not os.path.exists(meta) and now - os.path.getctime(d) < 30:
                try:
                    with open(meta, "w", encoding="utf-8") as f:
                        json.dump({"chat_id": chat_id, "is_group": is_group, "pushed": False}, f)
                except Exception:
                    pass

    async def _consume_push_file(self, path):
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                token = json.load(f)
            chat_id, is_group, text = _normalize_push_token(token)
            if not chat_id or not text:
                os.remove(path)
                return
            ok = await self._push(chat_id, is_group, text, push_id=str(token.get("push_id") or uuid.uuid4().hex[:12]))
            if ok:
                os.remove(path)
            else:
                print(f"[IPC] push file kept for retry: {path}")
        except Exception as e:
            print(f"[IPC] push file error {path}: {e}")

    async def _consume_push_jsonl(self, path):
        if not os.path.isfile(path):
            return
        keep = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    token = json.loads(line)
                    chat_id, is_group, text = _normalize_push_token(token)
                    if chat_id and text:
                        ok = await self._push(chat_id, is_group, text, push_id=str(token.get("push_id") or uuid.uuid4().hex[:12]))
                        if not ok:
                            keep.append(line)
                    else:
                        print(f"[IPC] dropping invalid push jsonl item: {line[:120]}")
                except Exception as e:
                    keep.append(line)
                    print(f"[IPC] push jsonl item error: {e}")
            if keep:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(keep) + "\n")
            else:
                open(path, "w", encoding="utf-8").close()
        except Exception as e:
            print(f"[IPC] push jsonl error {path}: {e}")

    async def _push_monitor(self):
        """Background coroutine: push sub-agent progress + queued messages."""
        _last_ping = {}  # sub_agent_id → last progress push time
        while True:
            await asyncio.sleep(5)

            if os.path.isdir(SUB_AGENTS_DIR):
                for name in os.listdir(SUB_AGENTS_DIR):
                    d = os.path.join(SUB_AGENTS_DIR, name)
                    meta_file = os.path.join(d, ".ipc_meta.json")
                    if not os.path.isfile(meta_file):
                        continue
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                    except Exception:
                        continue

                    status_file = os.path.join(d, "status.json")
                    if not os.path.isfile(status_file):
                        continue
                    try:
                        with open(status_file, "r", encoding="utf-8") as f:
                            st = json.load(f)
                    except Exception:
                        continue

                    chat_id = meta.get("chat_id", "")
                    is_group = meta.get("is_group", False)
                    if not chat_id:
                        continue

                    terminal = st.get("status") in ("done", "error", "timeout")

                    if terminal:
                        if meta.get("pushed"):
                            continue
                        result = st.get("result", "")
                        full_path = os.path.join(d, "full_response.txt")
                        if os.path.exists(full_path):
                            with open(full_path, "r", encoding="utf-8") as f:
                                result = f.read()
                        if st.get("status") == "done":
                            push_text = f"子代理任务完成\n{result[:1500]}"
                        elif st.get("status") == "error":
                            push_text = f"子代理执行出错\n{result[:500]}"
                        else:
                            push_text = "子代理超时"
                        now = time.time()
                        if not _can_attempt_push(meta, now=now):
                            continue
                        push_id = meta.get("push_id") or uuid.uuid4().hex[:12]
                        ok = await self._push(chat_id, is_group, push_text, push_id=push_id)
                        if ok:
                            _mark_push_success(meta_file, meta, push_id=push_id, now=now)
                        else:
                            _mark_push_failure(meta_file, meta, "napcat send_msg failed or timed out", now=now)
                    else:
                        progress = st.get("progress") or st.get("result") or ""
                        turns = st.get("turns", 0)
                        last = _last_ping.get(name, 0)
                        now = time.time()
                        if progress and now - last > 30:
                            push_text = f"子代理工作中 (已{turns}轮): {progress[:200]}"
                            ok = await self._push(chat_id, is_group, push_text, push_id=uuid.uuid4().hex[:12])
                            if ok:
                                _last_ping[name] = now

            if os.path.isdir(SCHEDULER_DONE_DIR):
                for fname in os.listdir(SCHEDULER_DONE_DIR):
                    if not fname.endswith(".push"):
                        continue
                    await self._consume_push_file(os.path.join(SCHEDULER_DONE_DIR, fname))

            await self._consume_push_file(LEGACY_REPORT_PUSH_FILE)
            await self._consume_push_jsonl(MONITOR_PUSH_QUEUE_FILE)

    async def _push(self, chat_id, is_group, text, push_id=None, ack_timeout=12):
        """Send an unsolicited push message to napcat and wait for send_msg ack."""
        push_id = push_id or uuid.uuid4().hex[:12]
        with self._lock:
            writer = self._push_writer
        if writer is None:
            print(f"[IPC] push skipped (no napcat connection): chat={chat_id}")
            return False

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self._lock:
            self._pending_push_acks[push_id] = fut

        try:
            await self._send_json(writer, {
                "type": "push",
                "push_id": push_id,
                "chat_id": chat_id,
                "is_group": is_group,
                "text": text,
            })
            ack = await asyncio.wait_for(fut, timeout=ack_timeout)
            ok = bool(ack.get("ok"))
            if ok:
                print(f"[IPC] push ack ok {push_id}: chat={chat_id}", flush=True)
            else:
                print(f"[IPC] push nack {push_id}: {ack.get('error', '')}", flush=True)
            return ok
        except asyncio.TimeoutError:
            print(f"[IPC] push ack timeout {push_id}: chat={chat_id}")
            return False
        except Exception as e:
            print(f"[IPC] push error {push_id}: {e}")
            return False
        finally:
            with self._lock:
                self._pending_push_acks.pop(push_id, None)

    @staticmethod
    def _save_meta(path, meta):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except Exception:
            pass

    @staticmethod
    async def _send_json(writer, obj):
        writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
