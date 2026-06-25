"""IPC TCP server — bridges QQ messages to the main agent process.

NapCat maintains ONE persistent TCP connection for all communication.

Protocol (JSON-line over TCP, 127.0.0.1:9001):
  Request:   {"type":"req","id":"<short_id>","content":"<msg>","chat_id":"<qq_id>","is_group":bool}
  Chunk:     {"type":"chunk","id":"<short_id>","text":"<partial>"}
  Response:  {"type":"done","id":"<short_id>","text":"<final>","error":null|<str>}
  Push:      {"type":"push","chat_id":"<qq_id>","is_group":bool,"text":"<msg>"}
"""

import asyncio, json, queue, threading, os, glob, time, uuid, traceback, hashlib

IPC_HOST = "127.0.0.1"
IPC_PORT = 9001
SUB_AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp", "sub_agents")
SCHEDULER_DONE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sche_tasks", "done")

# ── Simple semantic cache ──
_CACHE = {}  # hash → {reply, ttl}
_CACHE_MAX = 256
_CACHE_TTL = 300  # 5 minutes


def _cache_key(text):
    """Simple cache key: lowercased stripped text, hashed."""
    # Use a very simple "normalize and hash" approach
    t = text.lower().strip()
    # Remove common prefixes that shouldn't affect cache
    t = t.lstrip("?？!！。，,. ")
    if len(t) > 200:
        # Only use hash for long inputs
        return "long:" + hashlib.md5(t.encode()).hexdigest()
    return "exact:" + hashlib.md5(t.encode()).hexdigest()


def _check_cache(text):
    """Returns cached reply if found and not expired, else None."""
    now = time.time()
    dead = []
    hit = None
    for k, v in list(_CACHE.items()):
        if now > v.get("ttl", 0):
            dead.append(k)
        elif k == _cache_key(text):
            hit = v["reply"]
    for k in dead:
        _CACHE.pop(k, None)
    return hit


def _set_cache(text, reply):
    if len(_CACHE) >= _CACHE_MAX:
        # Remove oldest
        for k in list(_CACHE)[:len(_CACHE) - _CACHE_MAX + 32]:
            _CACHE.pop(k, None)
    _CACHE[_cache_key(text)] = {"reply": reply, "ttl": time.time() + _CACHE_TTL}


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


class IpcServer:
    def __init__(self, agent, host=IPC_HOST, port=IPC_PORT):
        self.agent = agent
        self.host = host
        self.port = port
        self._push_writer = None  # persistent napcat connection for pushes
        self._lock = threading.Lock()
        self._loop = None
        self._thread = None

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
        try:
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=600)
                if not raw:
                    break  # connection closed
                msg = json.loads(raw.decode("utf-8").strip())
                if msg.get("type") == "req":
                    asyncio.create_task(self._process_request(msg, reader, writer))
                elif msg.get("type") == "register":
                    with self._lock:
                        self._push_writer = writer
                    print(f"[IPC] napcat registered for push")
                elif msg.get("type") == "ping":
                    pass  # keepalive, no response needed
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, Exception) as e:
            print(f"[IPC] connection closed: {e}")
        finally:
            with self._lock:
                # Only clear if generation hasn't changed (newer connection already registered)
                if self._push_writer is writer:
                    self._push_writer = None
                    print("[IPC] napcat push connection lost")

    async def _process_request(self, msg, reader, writer):
        """Process a single request on the persistent connection."""
        req_id = msg.get("id", uuid.uuid4().hex[:8])
        content = msg.get("content", "")
        chat_id = msg.get("chat_id", "")
        is_group = msg.get("is_group", False)

        if not content:
            await self._send_json(writer, {"type": "done", "id": req_id, "text": "", "error": "empty message"})
            return

        # Check semantic cache for short/common queries
        if len(content) < 100:
            cached = _check_cache(content)
            if cached:
                print(f"[IPC] cache hit for: {content[:60]}...")
                await self._send_json(writer, {"type": "done", "id": req_id, "text": cached, "error": None, "cached": True})
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
        self.agent.task_queue.put({
            "query": content,
            "source": "ipc",
            "chat_id": chat_id,
            "images": [],
            "output": dq,
            "max_turns": 6,
            "reset_history": False,  # Session-isolated
            "personality": personality,
        })

        loop = asyncio.get_running_loop()
        full_text = ""
        while True:
            item = await loop.run_in_executor(None, dq.get)
            if "next" in item:
                chunk = item.get("next", "")
                if chunk:
                    full_text += chunk
                    await self._send_json(writer, {"type": "chunk", "id": req_id, "text": chunk})
            if "done" in item:
                full_text = item.get("done", "")
                break

        await self._send_json(writer, {"type": "done", "id": req_id, "text": full_text, "error": None})

        # Cache short replies (not error, not empty)
        if len(content) < 100 and full_text and len(full_text) > 5:
            _set_cache(content, full_text)

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

    async def _push_monitor(self):
        """Background coroutine: push sub-agent progress + completion results."""
        _last_ping = {}  # sub_agent_id → last progress push time
        while True:
            await asyncio.sleep(5)
            if not os.path.isdir(SUB_AGENTS_DIR):
                continue
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
                    # Read full response
                    result = st.get("result", "")
                    full_path = os.path.join(d, "full_response.txt")
                    if os.path.exists(full_path):
                        with open(full_path, "r", encoding="utf-8") as f:
                            result = f.read()

                    if st.get("status") == "done":
                        push_text = f"✅ 子代理任务完成！\n{result[:1500]}"
                    elif st.get("status") == "error":
                        push_text = f"❌ 子代理执行出错\n{result[:500]}"
                    else:
                        push_text = "⏱️ 子代理超时"

                    await self._push(chat_id, is_group, push_text)
                    meta["pushed"] = True
                    self._save_meta(meta_file, meta)
                else:
                    # Push progress updates (limit to once per 30s per sub-agent)
                    progress = st.get("progress") or st.get("result") or ""
                    turns = st.get("turns", 0)
                    last = _last_ping.get(name, 0)
                    now = time.time()
                    if progress and now - last > 30:
                        push_text = f"⏳ 子代理工作中 (已{turns}轮): {progress[:200]}"
                        await self._push(chat_id, is_group, push_text)
                        _last_ping[name] = now

        # ── Schedule push tokens: .push files written by scheduler tasks ──
        push_dir = SCHEDULER_DONE_DIR
        if os.path.isdir(push_dir):
            for fname in os.listdir(push_dir):
                if not fname.endswith(".push"):
                    continue
                fpath = os.path.join(push_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        token = json.load(f)
                    text = token.get("text", "")
                    chat_id = token.get("chat_id", "")
                    is_group = token.get("is_group", False)
                    if chat_id and text:
                        await self._push(chat_id, is_group, text)
                    os.remove(fpath)
                except Exception:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass

    async def _push(self, chat_id, is_group, text):
        """Send an unsolicited push message to napcat."""
        with self._lock:
            writer = self._push_writer
        if writer is None:
            print(f"[IPC] push skipped (no napcat connection): chat={chat_id}")
            return
        try:
            await self._send_json(writer, {
                "type": "push", "chat_id": chat_id,
                "is_group": is_group, "text": text
            })
        except Exception as e:
            print(f"[IPC] push error: {e}")

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
