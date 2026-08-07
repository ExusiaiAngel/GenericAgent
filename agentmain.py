import os, sys, threading, queue, time, json, re, random, locale, tempfile
from copy import deepcopy
os.environ.setdefault('GA_LANG', 'zh' if any(k in (locale.getlocale()[0] or '').lower() for k in ('zh', 'chinese')) else 'en')
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
elif hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(errors='replace')
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
elif hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(errors='replace')
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from llmcore import reload_mykeys, ToolClient, MixinSession, NativeToolClient, NativeClaudeSession, NativeOAISession, resolve_client
from agent_loop import agent_runner_loop
try:
    from plugins.hooks import discover_and_load; discover_and_load()
except Exception: pass
from ga import GenericAgentHandler, smart_format, get_global_memory, format_error, consume_file

script_dir = os.path.dirname(os.path.abspath(__file__))
TASK_QUEUE_MAX = max(8, int(os.environ.get("GENERICAGENT_TASK_QUEUE_MAX", "64")))
MEMORY_SETTLEMENT_TOOL_NAMES = frozenset({
    "file_read", "file_patch", "file_write", "start_long_term_update",
})


def _should_settle_memory(task, terminal, handler):
    enabled = os.environ.get("GENERICAGENT_AUTO_MEMORY", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return False
    return (
        (terminal or {}).get("state") == "completed"
        and (task or {}).get("chat_route") == "long_task"
        and int(getattr(handler, "current_turn", 0) or 0) > 1
    )


def _normalize_ipc_terminal(task, terminal):
    """Convert raw verbose runner text into a truthful IPC terminal payload."""
    if (task or {}).get("source") != "ipc" or (terminal or {}).get("state") != "completed":
        return terminal
    from frontends.shared.chatapp_common import _extract_final_answer
    visible = _extract_final_answer(terminal.get("text", "")).strip()
    normalized = dict(terminal)
    if visible:
        normalized["text"] = visible
        return normalized
    normalized.update({
        "state": "failed",
        "text": "",
        "error": "任务执行了工具操作，但模型没有生成可发送的最终答案。",
    })
    return normalized


def _stream_delta(full_response, last_position):
    """Return the unsent suffix for append-only display/IPC consumers."""
    return str(full_response or "")[max(0, int(last_position or 0)):]


def _suppress_tool_stdout(source, no_print=False):
    """Keep remote-chat tool payloads out of the systemd journal."""
    return bool(no_print) or source == "ipc"


def _reset_model_context(agent):
    """Clear every history surface used by the active model client."""
    agent.history = []
    client = agent.llmclient
    backend = getattr(client, "backend", None)
    if backend is not None and hasattr(backend, "history"):
        backend.history = []
    if hasattr(client, "last_tools"):
        client.last_tools = ""
    if hasattr(client, "_pending_tool_ids"):
        client._pending_tool_ids = []


def _should_inject_context_checkpoint(source):
    """Scheduled jobs are isolated runs, not continuations of user context."""
    return source != "reflect"


def _save_long_user_prompt(text, temp_dir):
    """Atomically reserve a private unique file for an oversized prompt."""
    os.makedirs(temp_dir, mode=0o750, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=temp_dir,
        prefix="user_prompt_",
        suffix=".md",
        delete=False,
    ) as handle:
        handle.write(str(text))
        path = handle.name
    os.chmod(path, 0o640)
    return path


def runtime_service_policy(task_mode=False):
    enabled = not bool(task_mode)
    return {"watchdog": enabled, "ipc": enabled}

def resolve_task_dir(task_arg, script_dir=script_dir):
    """Resolve --task argument to an absolute normalized directory path.

    Returns absolute normalized path. Raises ValueError if empty/whitespace.
    - Absolute path: normalized as-is.
    - Exact '.' / '..' or './' / '../' prefix: resolved relative to script_dir.
    - All other names (including '..evil'): placed under script_dir/temp/.
    """
    task_arg_stripped = task_arg.strip() if task_arg else ''
    if not task_arg_stripped:
        raise ValueError('--task argument cannot be empty or whitespace-only')

    if os.path.isabs(task_arg):
        return os.path.abspath(os.path.normpath(task_arg))

    if task_arg in ('.', '..') or task_arg.startswith('./') or task_arg.startswith('../'):
        return os.path.abspath(os.path.normpath(os.path.join(script_dir, task_arg)))

    return os.path.abspath(os.path.normpath(os.path.join(script_dir, 'temp', task_arg)))

def load_tool_schema(suffix=''):
    global TOOLS_SCHEMA
    with open(os.path.join(script_dir, f'assets/tools_schema{suffix}.json'), 'r', encoding='utf-8') as _f:
        TS = _f.read()
    TOOLS_SCHEMA = json.loads(TS if os.name == 'nt' else TS.replace('powershell', 'bash'))
load_tool_schema()

lang_suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
mem_dir = os.path.join(script_dir, 'memory')
if not os.path.exists(mem_dir): os.makedirs(mem_dir)
mem_txt = os.path.join(mem_dir, 'global_mem.txt')
if not os.path.exists(mem_txt):
    with open(mem_txt, 'w', encoding='utf-8') as f: f.write('# [Global Memory - L2]\n')
mem_insight = os.path.join(mem_dir, 'global_mem_insight.txt')
if not os.path.exists(mem_insight):
    t = os.path.join(script_dir, f'assets/global_mem_insight_template{lang_suffix}.txt')
    if os.path.exists(t):
        with open(t, encoding='utf-8') as src:
            with open(mem_insight, 'w', encoding='utf-8') as dst:
                dst.write(src.read())
    else:
        with open(mem_insight, 'w', encoding='utf-8') as f: f.write('')
cdp_cfg = os.path.join(script_dir, 'assets/tmwd_cdp_bridge/config.js')
if not os.path.exists(cdp_cfg):
    try:
        os.makedirs(os.path.dirname(cdp_cfg), exist_ok=True)
        with open(cdp_cfg, 'w', encoding='utf-8') as f:
            f.write(f"const TID = '__ljq_{hex(random.randint(0, 99999999))[2:8]}';")
    except Exception as e: print(f'[WARN] CDP config init failed: {e} — advanced web features (tmwebdriver) will be unavailable.')

def get_system_prompt():
    with open(os.path.join(script_dir, f'assets/sys_prompt{lang_suffix}.txt'), 'r', encoding='utf-8') as f: prompt = f.read()
    prompt += f"\nToday: {time.strftime('%Y-%m-%d %a')}\n"
    prompt += get_global_memory()
    return prompt

class GenericAgent:
    def __init__(self, start_watchdog=True):
        os.makedirs(os.path.join(script_dir, 'temp'), exist_ok=True)
        self.lock = threading.RLock()
        self.task_dir = None
        self.history = []; self.handler = None;
        self.MAX_HISTORY = 60
        self.task_queue = queue.Queue(maxsize=TASK_QUEUE_MAX)
        self.is_running = False; self.stop_sig = False; self.llm_no = 0;
        self.inc_out = False; self.verbose = True; self.show_mode = 'text'
        self.peer_hint = True
        self.force_non_stream = False
        self.session_histories: dict[str, list] = {}
        self.session_backend_histories: dict[str, list] = {}
        self.session_generations: dict[str, int] = {}
        self.session_configs: dict[str, dict] = {}
        from session_store import SessionStore
        self.session_store = SessionStore(
            os.environ.get(
                "GENERICAGENT_SESSION_DB",
                os.path.join(script_dir, "temp", "sessions.db"),
            )
        )
        from skill_manager import SkillManager
        self.skill_manager = SkillManager(
            os.path.join(script_dir, "temp", "skill_proposals"),
            os.path.join(script_dir, "memory", "skills"),
        )
        from change_approval import ChangeApprovalManager, get_change_state_root
        configured_change_roots = [script_dir]
        configured_change_roots.extend(
            item.strip()
            for item in re.split(r"[;,]", os.environ.get("GENERICAGENT_CHANGE_ROOTS", ""))
            if item.strip()
        )
        self.change_approval = ChangeApprovalManager(
            get_change_state_root(),
            configured_change_roots,
        )
        self.active_chat_id = None
        self._active_generation = 0
        self.active_task = None
        self._discard_active_task = None
        self._active_task_snapshot = None
        logid = f'{(time.time_ns() + random.randrange(1_000_000)) % 1_000_000:06d}'
        self.log_path = os.path.join(script_dir, f'temp/model_responses/model_responses_{logid}.txt')
        self.load_llm_sessions()
        self.last_error = None  # set by run() on LLM exception
        # ── 启动 Context Watchdog 守护线程 ──
        try:
            if not start_watchdog:
                raise FileNotFoundError("disabled for task mode")
            wd_path = os.path.join(script_dir, 'temp', 'context_watchdog.py')
            if os.path.exists(wd_path):
                import runpy
                self._watchdog = threading.Thread(target=lambda: runpy.run_path(wd_path, run_name='__watchdog__'), 
                                                   daemon=True, name='context_watchdog')
                self._watchdog.start()
                print(f"[Watchdog] Context Watchdog 已启动 (阈值={30}轮)")
            else:
                print(f"[Watchdog] 未找到 {wd_path}，跳过")
        except FileNotFoundError as e:
            if start_watchdog:
                print(f"[Watchdog] 未启动: {e}")
        except Exception as e:
            print(f"[Watchdog] 启动失败: {e}")
        # ── Watchdog 结束 ──

    def load_llm_sessions(self):
        mykeys, changed = reload_mykeys()
        if not changed and hasattr(self, 'llmclients'): return
        try: oldhistory = self.llmclient.backend.history
        except: oldhistory = None
        llm_sessions = []
        for k, cfg in mykeys.items():
            if not any(x in k for x in ['api', 'config', 'cookie']): continue
            try:
                if 'mixin' in k: llm_sessions += [{'mixin_cfg': cfg}]
                elif c := resolve_client(k): llm_sessions += [c]
            except: pass
        for i, s in enumerate(llm_sessions):
            if isinstance(s, dict) and 'mixin_cfg' in s:
                try:
                    mixin = MixinSession(llm_sessions, s['mixin_cfg'])
                    if isinstance(mixin._sessions[0], (NativeClaudeSession, NativeOAISession)): llm_sessions[i] = NativeToolClient(mixin)
                    else: llm_sessions[i] = ToolClient(mixin)
                except Exception as e: print(f'\n\n\n[ERROR] Failed to init MixinSession with cfg {s["mixin_cfg"]}: {e}!!!\n\n')
        if not llm_sessions:
            raise RuntimeError(
                "No LLM providers configured. Set a provider API key in "
                f"{os.path.join(script_dir, '.env')} (for example DEEPSEEK_API_KEY)."
            )
        self.llmclients = llm_sessions
        self.llmclient = self.llmclients[self.llm_no%len(self.llmclients)]
        if oldhistory: self.llmclient.backend.history = oldhistory
    
    def next_llm(self, n=-1):
        self.load_llm_sessions()
        self.llm_no = ((self.llm_no + 1) if n < 0 else n) % len(self.llmclients)
        lastc = self.llmclient
        self.llmclient = self.llmclients[self.llm_no]
        try: self.llmclient.backend.history = lastc.backend.history
        except: raise Exception('[ERROR] BAD Mixin config: Check your mykey.py')
        self.llmclient.last_tools = ''
        name = self.get_llm_name(model=True)
        if 'glm' in name or 'minimax' in name or 'kimi' in name: load_tool_schema('_cn')
        else: load_tool_schema()
    def list_llms(self): 
        self.load_llm_sessions()
        return [(i, self.get_llm_name(b), i == self.llm_no) for i, b in enumerate(self.llmclients)]
    def get_llm_name(self, b=None, model=False):
        b = self.llmclient if b is None else b
        if isinstance(b, dict): return 'BADCONFIG_MIXIN'
        if model: return b.backend.model.lower()
        return f"{type(b.backend).__name__}/{b.backend.name}"

    def get_ctx_multiplier(self):
        return getattr(self.llmclient.backend, 'maxlen_multiplier', 1.0)

    def abort(self):
        if not self.is_running: return
        print('Abort current task...')
        self.stop_sig = True
        if self.handler is not None: self.handler.code_stop_signal.append(1)

    def abort_chat_task(self, chat_id):
        """Abort only when *chat_id* owns the currently executing task."""
        chat_id = str(chat_id or "")
        with self.lock:
            active = dict(self.active_task or {})
            if (
                not self.is_running
                or not active
                or str(active.get("chat_id") or "") != chat_id
            ):
                return False
            generation = int(active.get("generation", 0))
            self._discard_active_task = (chat_id, generation)
        self.abort()
        return True

    def _capture_chat_snapshot(self, chat_id, generation):
        chat_id = str(chat_id or "")
        with self.lock:
            return {
                "chat_id": chat_id,
                "generation": int(generation),
                "history": list(self.history),
                "backend_history": deepcopy(
                    getattr(self.llmclient.backend, "history", [])
                ),
            }

    def _restore_chat_snapshot(self, snapshot):
        """Rollback an aborted turn without erasing earlier conversation."""
        if not snapshot:
            return False
        chat_id = str(snapshot.get("chat_id") or "")
        generation = int(snapshot.get("generation", 0))
        with self.lock:
            if self.session_generations.get(chat_id, 0) != generation:
                return False
            history = list(snapshot.get("history") or [])
            backend_history = deepcopy(snapshot.get("backend_history") or [])
            self.session_histories[chat_id] = history
            self.session_backend_histories[chat_id] = backend_history
            if self.active_chat_id == chat_id and self._active_generation == generation:
                self.history = list(history)
                self.llmclient.backend.history = deepcopy(backend_history)
                if hasattr(self.llmclient, "_pending_tool_ids"):
                    self.llmclient._pending_tool_ids = []
            return True

    def _save_active_chat_session(self, chat_id=None, generation=None):
        chat_id = str(chat_id or self.active_chat_id or "")
        if not chat_id:
            return False
        current_generation = self.session_generations.get(chat_id, 0)
        if generation is not None and int(generation) != current_generation:
            return False
        with self.lock:
            self.session_backend_histories[chat_id] = deepcopy(
                getattr(self.llmclient.backend, "history", [])
            )
            self.session_histories[chat_id] = list(self.history)
        return True

    def _activate_chat_session(self, chat_id, generation=None, identity=None):
        chat_id = str(chat_id or "")
        with self.lock:
            if self.active_chat_id and self.active_chat_id != chat_id:
                self._save_active_chat_session(
                    self.active_chat_id,
                    generation=self._active_generation,
                )
            current_generation = self.session_generations.get(chat_id, 0)
            if generation is None:
                generation = current_generation
            if identity and chat_id not in self.session_histories:
                try:
                    from session_store import ConversationIdentity
                    cid = ConversationIdentity(**{
                        key: identity.get(key, "")
                        for key in ("platform", "account", "conversation", "actor")
                    })
                    durable_generation = self.session_store.generation(cid)
                    if generation == 0:
                        generation = durable_generation
                        self.session_generations[chat_id] = durable_generation
                    recent = self.session_store.recent(cid, generation, limit=20)
                    if recent:
                        self.session_histories[chat_id] = [
                            f"[{'USER' if row['role'] == 'user' else 'Agent'}]: {row['content']}"
                            for row in recent
                        ]
                        self.session_backend_histories[chat_id] = [
                            {"role": row["role"], "content": [{"type": "text", "text": row["content"]}]}
                            for row in recent
                        ]
                except Exception as restore_error:
                    print(f"[SESSION] restore_error={format_error(restore_error)}")
            self.active_chat_id = chat_id
            self._active_generation = int(generation)
            self.history = list(self.session_histories.get(chat_id, []))
            self.llmclient.backend.history = deepcopy(
                self.session_backend_histories.get(chat_id, [])
            )
            if hasattr(self.llmclient, "_pending_tool_ids"):
                self.llmclient._pending_tool_ids = []
            return int(generation)

    def reset_chat_session(self, chat_id):
        chat_id = str(chat_id or "")
        with self.lock:
            generation = self.session_generations.get(chat_id, 0) + 1
            self.session_generations[chat_id] = generation
            self.session_histories.pop(chat_id, None)
            self.session_backend_histories.pop(chat_id, None)
            if self.active_chat_id == chat_id:
                self._active_generation = generation
                self.history = []
                self.llmclient.backend.history = []
                if hasattr(self.llmclient, "_pending_tool_ids"):
                    self.llmclient._pending_tool_ids = []
                self.handler = None
            return generation

    def reset_conversation(self, chat_id, identity=None):
        generation = self.reset_chat_session(chat_id)
        if identity:
            try:
                from session_store import ConversationIdentity
                cid = ConversationIdentity(**{
                    key: identity.get(key, "")
                    for key in ("platform", "account", "conversation", "actor")
                })
                self.session_store.set_generation(cid, generation)
            except Exception as store_error:
                print(f"[SESSION] reset_store_error={format_error(store_error)}")
        return generation

    def task_status(self, chat_id=""):
        with self.lock:
            active = dict(self.active_task or {})
            queued = self.task_queue.qsize()
        if active:
            elapsed = max(0, int(time.time() - active.get("started_at", time.time())))
            return {
                "state": "running",
                "text": f"正在运行 {elapsed}s（第 {active.get('turn', 0)} 轮）：{active.get('query', '')[:80]}",
            }
        if queued:
            return {"state": "queued", "text": f"当前有 {queued} 个任务排队。"}
        return {"state": "idle", "text": "当前空闲，没有正在执行的任务。"}

    def _run_memory_settlement(self, task, source_handler):
        """Run a hidden, bounded, memory-only settlement pass.

        The just-completed backend history is restored in all cases so this
        internal pass cannot leak into the user's chat session or output.
        """
        backend_history = deepcopy(self.llmclient.backend.history)
        try:
            tools = [
                tool for tool in TOOLS_SCHEMA
                if (tool.get("function") or {}).get("name")
                in MEMORY_SETTLEMENT_TOOL_NAMES
            ]
            handler = GenericAgentHandler(
                self,
                list(getattr(source_handler, "history_info", []) or []),
                os.path.join(script_dir, "temp"),
                allow_inline_eval=False,
                memory_only=True,
            )
            handler.print = lambda *args, **kwargs: None
            settlement_prompt = (
                "这是刚完成任务的隐藏记忆结算，不要复述用户答案。先调用 "
                "start_long_term_update 读取 L0，再决定是否需要最小 patch。"
                "只保存工具结果已经验证、跨会话稳定、未来难以快速重建的信息；"
                "拒绝猜测、计划、临时状态、PID/时间戳、密钥和通用常识。"
                "没有合格信息时直接回答 NO_MEMORY，不调用写工具。"
            )
            runner = agent_runner_loop(
                self.llmclient,
                get_system_prompt() + "\n[Memory settlement: hidden from user]",
                settlement_prompt,
                handler,
                tools,
                max_turns=4,
                verbose=False,
                yield_info=False,
            )
            while True:
                try:
                    next(runner)
                except StopIteration as stopped:
                    return stopped.value or {"result": "MAX_TURNS_EXCEEDED"}
        finally:
            self.llmclient.backend.history = backend_history
            # 结算轮若被 max_turns 截断会残留孤儿 tool_use_id，
            # 不清空则下轮用户对话被注入空 tool_result 块（API 400）。
            if hasattr(self.llmclient, "_pending_tool_ids"):
                self.llmclient._pending_tool_ids = []

    def _repair_missing_final_answer(self, task, handler):
        """Ask for one tool-free user answer after a tool-only completion.

        The repair pass is hidden: it shares the main LLM client, so both the
        backend history and handler.history_info are restored afterwards and
        only a non-empty final answer is merged back in.
        """
        prompt = (
            "刚才的任务工具操作已经结束，但缺少用户可见的最终答复。"
            "现在禁止调用任何工具，只根据已经完成的结果给出简洁、真实的最终答案。"
            "不要输出思考、summary 标签、工具日志或下一步动作句。"
        )
        backend_history = deepcopy(self.llmclient.backend.history)
        history_info_backup = list(getattr(handler, "history_info", []) or [])
        chunks = []
        try:
            runner = agent_runner_loop(
                self.llmclient,
                get_system_prompt() + "\n[Final answer repair: tools disabled]",
                prompt,
                handler,
                [],
                max_turns=1,
                verbose=False,
                yield_info=False,
            )
            while True:
                try:
                    chunk = next(runner)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
                except StopIteration:
                    break
        finally:
            self.llmclient.backend.history = backend_history
            if hasattr(self.llmclient, "_pending_tool_ids"):
                self.llmclient._pending_tool_ids = []
            handler.history_info = history_info_backup
        answer = "".join(chunks).strip()
        if answer:
            self.llmclient.backend.history.append(
                {"role": "assistant", "content": [{"type": "text", "text": answer}]}
            )
        return answer

    def _publish_terminal_and_settle(
        self, task, terminal, handler, display_queue, *, source, turn,
        outputs, original_query, generation,
    ):
        """Publish the user terminal before the serialized hidden settlement."""
        if (
            source == "ipc" and terminal.get("state") == "completed"
            and task.get("conversation_identity")
        ):
            try:
                from session_store import ConversationIdentity
                cid = ConversationIdentity(**{
                    key: task["conversation_identity"].get(key, "")
                    for key in ("platform", "account", "conversation", "actor")
                })
                self.session_store.record_exchange(
                    cid, generation, task.get("request_id", ""),
                    original_query, terminal.get("text", ""),
                )
            except Exception as store_error:
                print(f"[SESSION] persist_error={format_error(store_error)}")

        display_queue.put({
            "done": terminal.get("text", ""), "terminal": terminal,
            "source": source, "turn": turn, "outputs": list(outputs),
        })

        # Keep settlement serialized because it temporarily uses the shared
        # model client and history.  It intentionally runs after publication,
        # so it can delay the next queued task but never the current reply.
        if _should_settle_memory(task, terminal, handler):
            try:
                settlement = self._run_memory_settlement(task, handler)
                print(f"[MEMORY-AUDIT] settlement={settlement.get('result', 'unknown')}")
            except Exception as settlement_error:
                print(f"[MEMORY-AUDIT] settlement_error={format_error(settlement_error)}")
            
    def put_task(self, query, source="user", images=None, max_turns=None, display_queue=None, reset_history=False, identity=None):
        if display_queue is None:
            display_queue = queue.Queue()
        from frontends.shared.task_protocol import turns_for_source
        reset_history = bool(reset_history or source == "reflect")
        max_turns = turns_for_source(source, requested=max_turns)
        try:
            self.task_queue.put_nowait({"query": query, "source": source, "images": images or [], "output": display_queue, "max_turns": max_turns, "reset_history": reset_history, "conversation_identity": dict(identity or {})})
        except queue.Full:
            display_queue.put({
                "done": "",
                "terminal": {
                    "state": "failed", "text": "",
                    "error": "任务队列繁忙，请稍后重试。",
                    "question": "", "candidates": [],
                },
                "source": source,
            })
        return display_queue

    # i know it is dangerous, but raw_query is dangerous enough it doesn't enlarge
    def _handle_slash_cmd(self, raw_query, display_queue):
        if not raw_query.startswith('/'): return raw_query
        if _sm := re.match(r'/session\.(\w+)=(.*)', raw_query.strip()):
            k, v = _sm.group(1), _sm.group(2)
            vfile = os.path.abspath(os.path.normpath(os.path.join(script_dir, 'temp', v)))
            temp_dir = os.path.abspath(os.path.join(script_dir, 'temp'))
            if os.path.isfile(vfile) and vfile.startswith(temp_dir + os.sep):
                with open(vfile, encoding='utf-8') as f: v = f.read().strip()
            try: v = json.loads(v)  # cover number parsing
            except (json.JSONDecodeError, ValueError): pass
            setattr(self.llmclient.backend, k, v)
            display_queue.put({'done': smart_format(f"✅ session.{k} = {repr(v)}", max_str_len=500), 'source': 'system'})
            return None
        if raw_query.strip() == '/resume':
            return r'帮我看看最近有哪些会话可以恢复。读model_responses/目录，按修改时间取最近10个文件，从每个文件里找最后一个<history>...</history>块，用一句话总结每个会话在聊什么，列表给我选。注意读文件后要把字面的\n替换成真换行才能正确匹配。'
        return raw_query

    @staticmethod
    def _scan_available_sops():
        """Return shared SOP governance docs plus executable top-level SOPs."""
        mem_dir = os.path.join(script_dir, 'memory')
        try:
            shared = [
                name for name in ('SOP_EXECUTION_CONTRACT.md', 'SOP_CATALOG.md')
                if os.path.isfile(os.path.join(mem_dir, name))
            ]
            domain_sops = sorted(
                name for name in os.listdir(mem_dir)
                if name.endswith('_sop.md')
                and os.path.isfile(os.path.join(mem_dir, name))
            )
            return shared + domain_sops
        except OSError:
            return []

    @staticmethod
    def _inject_context_checkpoint() -> str:
        """读取 checkpoint 并生成上下文注入块。如无 checkpoint 返回空字符串"""
        ck_file = os.path.join(script_dir, 'temp', 'context_checkpoints', 'latest.json')
        if not os.path.exists(ck_file):
            return ""
        try:
            with open(ck_file) as f:
                index = json.load(f)
            ck_path = index.get("latest", "")
            if not ck_path or not os.path.exists(ck_path):
                return ""
            with open(ck_path) as f:
                ck = json.load(f)
            # 从 checkpoint 恢复上下文
            injection = "\n" + "="*50 + "\n[CONTEXT_ROLLOVER] 上下文快照恢复 — 此对话继承自前一个会话\n"
            injection += f"前会话轮次: {ck.get('turns', '?')}\n"
            if ck.get("last_turns"):
                # 提取最后几条消息的关键信息
                injection += f"最后对话摘要: {ck['last_turns'][-4:]}\n"
            if ck.get("available_sops"):
                injection += f"可用 SOP: {', '.join(ck['available_sops'][:15])}\n"
            if ck.get("available_tools"):
                injection += f"可用工具: {', '.join(ck['available_tools'][:20])}\n"
            injection += f"保存时间: {ck.get('timestamp', '?')}\n"
            injection += "="*50 + "\n"
            # 消费后删除最新 checkpoint 标记，避免重复注入
            try:
                os.remove(ck_file)
                # 同时标记任务为已处理
                done_dir = os.path.join(script_dir, 'sche_tasks', 'done')
                task_file = os.path.join(script_dir, 'sche_tasks', 'context_rollover.json')
                if os.path.exists(task_file):
                    os.makedirs(done_dir, exist_ok=True)
                    os.rename(task_file, os.path.join(done_dir, f"context_rollover_{ck.get('timestamp', 'done')}.json"))
            except:
                pass
            return injection
        except Exception as e:
            print(f"[Watchdog] checkpoint 注入失败: {e}")
            return ""

    def run(self):
        while True:
            task = self.task_queue.get()
            if isinstance(task, str): break
            raw_query, source, display_queue, max_turns = task["query"], task["source"], task["output"], task.get("max_turns", None)
            original_query = raw_query
            raw_query = self._handle_slash_cmd(raw_query, display_queue)
            if raw_query is None:
                self.task_queue.task_done(); continue
            self.is_running = True
            if len(raw_query) > 1500:
                task_file = _save_long_user_prompt(
                    raw_query, os.path.join(script_dir, 'temp')
                )
                raw_query = f'Long user prompt saved to {task_file}. Read and execute.'
            # ── Multi-session isolation ──
            chat_id = str(task.get("chat_id") or "")
            generation = int(task.get("generation", self.session_generations.get(chat_id, 0)))
            if chat_id:
                generation = self._activate_chat_session(
                    chat_id, generation=generation,
                    identity=task.get("conversation_identity"),
                )
            elif task.get("reset_history"):
                _reset_model_context(self)
                # 自动注入 checkpoint 上下文
                if _should_inject_context_checkpoint(source):
                    ck_injection = self._inject_context_checkpoint()
                    if ck_injection:
                        self.history.append(ck_injection)
            self._active_task_snapshot = (
                self._capture_chat_snapshot(chat_id, generation) if chat_id else None
            )
            # Auto model routing: simple queries → default model; complex → use existing selection
            if len(raw_query) < 80 and not any(kw in raw_query.lower() for kw in ['代码', '写', '分析', '搜索', '查找', '查', '修复', '改', 'create', 'write', 'search', 'analyze']):
                pass  # short chat — keep current model
            rquery = smart_format(raw_query.replace('\n', ' '), max_str_len=200)
            self.history.append(f"[USER]: {rquery}")
            if len(self.history) > self.MAX_HISTORY:
                cutoff = len(self.history) - self.MAX_HISTORY
                dropped = self.history[:cutoff]
                self.history = self.history[cutoff:]
                if dropped:
                    self.history.insert(0, f"[SYSTEM]: {len(dropped)} older conversation turns truncated for context limit.")

            sys_prompt = get_system_prompt() + getattr(self.llmclient.backend, 'extra_sys_prompt', '')
            if self.peer_hint: sys_prompt += f"\n[Peer] 用户提及其他会话/后台任务状态时: temp/model_responses/ (只找近期修改的文件尾部)\n"
            if source in ("ipc",):
                ipc_style = task.get("personality", "")
                chat_route = task.get("chat_route", "quick_chat")
                try:
                    from frontends.shared.chat_router import build_chat_style_prompt
                    platform = (task.get("conversation_identity") or {}).get("platform", "chat")
                    sys_prompt += build_chat_style_prompt(ipc_style, chat_route, platform)
                except Exception:
                    style_note = f"\n对话风格: {ipc_style}" if ipc_style else ""
                    sys_prompt += (
                        f"\n\n[聊天消息] 这是来自聊天平台的消息。{style_note}"
                        "回复要简短、直接。只有明确多步耗时任务才派发子代理。"
                    )
            else:
                # CodeAct-style + memory source tracking + cron registration
                sys_prompt += (
                    "\n\n[效率提示] 当需要多步操作时（如数据清洗、文件批处理、串行API调用等），"
                    "优先用 code_run 写一段完整的 Python 脚本一次性完成，而不是分多次 tool call。"
                    "例如：批量修改文件 → 用 Python 遍历+修改；数据爬取+分析 → 用 Python 一次性完成。"
                    "这能大幅减少 token 消耗和处理时间。"
                    "\n\n[记忆来源] 调用 start_long_term_update 保存事实时，请在 key_info 中注明信息来源。"
                    "格式：[来源: 文件路径/URL/对话摘要] → 事实内容。这样后续可以追溯信息来源。"
                    "\n\n[定时任务] 用户可以通过聊天消息请求注册定时任务。"
                    "用 spawn_subagent 派发子代理去 reflect/scheduler.py 注册 check() 函数。"
                    "调度器已加载 /opt/GenericAgent/reflect/scheduler.py。"
                )
            self.active_task = {
                "request_id": task.get("request_id", ""),
                "chat_id": chat_id,
                "generation": generation,
                "query": raw_query[:200],
                "route": task.get("chat_route", "quick_chat"),
                "queued_at": task.get("queued_at"),
                "started_at": time.time(),
                "turn": 0,
                "conversation_identity": dict(task.get("conversation_identity") or {}),
            }
            display_queue.put({'started': dict(self.active_task)})
            handler = GenericAgentHandler(
                self,
                self.history,
                os.path.join(script_dir, 'temp'),
                allow_inline_eval=source not in ("ipc", "task"),
            )
            if _suppress_tool_stdout(source, getattr(self, 'no_print', False)):
                handler.print = lambda *a, **k: None
            if task.get("continue_task") and self.handler and 'key_info' in self.handler.working:
                ki = re.sub(r'\n\[SYSTEM\] 此为.*?工作记忆[。\n]*', '', self.handler.working['key_info'])  # 去旧
                handler.working['key_info'] = ki
                handler.working['passed_sessions'] = ps = self.handler.working.get('passed_sessions', 0) + 1
                if ps > 0: handler.working['key_info'] += f'\n[SYSTEM] 此为 {ps} 个对话前设置的key_info，若已在新任务，先更新或清除工作记忆。\n'
            self.handler = handler  # although new handler, the **full** history is in llmclient, so it is full history!
            self.llmclient.log_path = self.log_path
            if self.force_non_stream:
                self.llmclient.backend.stream = False
                self.llmclient.backend.read_timeout = max(self.llmclient.backend.read_timeout, 1200)
            if source == "ipc":
                from frontends.shared.task_protocol import tool_schema_for_route
                task_tools = tool_schema_for_route(
                    TOOLS_SCHEMA, task.get("chat_route", "quick_chat")
                )
            else:
                task_tools = TOOLS_SCHEMA
            from frontends.shared.task_protocol import turns_for_source
            effective_max_turns = turns_for_source(
                source,
                requested=max_turns,
                route=task.get("chat_route", "quick_chat"),
            )
            gen = agent_runner_loop(self.llmclient, sys_prompt, raw_query, handler, task_tools,
                                    max_turns=effective_max_turns, verbose=self.verbose, yield_info=True)
            try:
                from frontends.shared.task_protocol import terminal_from_runner
                full_resp = ""; last_pos = 0; curr_turn = 0; turn_resps = []
                runner_result = None
                while True:
                    try:
                        chunk = next(gen)
                    except StopIteration as stopped:
                        runner_result = stopped.value or {"result": "MAX_TURNS_EXCEEDED"}
                        break
                    if consume_file(self.task_dir, '_stop'): self.abort() 
                    if self.stop_sig:
                        runner_result = {"result": "EXITED", "data": {"status": "STOPPED"}}
                        break
                    if isinstance(chunk, dict) and 'turn' in chunk: 
                        curr_turn = chunk['turn']; turn_resps.append('')
                        if self.active_task is not None: self.active_task['turn'] = curr_turn
                        continue
                    if not turn_resps: turn_resps.append('')
                    full_resp += chunk;  turn_resps[-1] += chunk
                    if len(full_resp) - last_pos > 30 or 'LLM Running' in chunk:
                        display_queue.put({'next': _stream_delta(full_resp, last_pos),
                                           'source': source, 'turn': curr_turn, 'outputs': turn_resps[-2:]})
                        last_pos = len(full_resp)
                if last_pos < len(full_resp):
                    display_queue.put({'next': _stream_delta(full_resp, last_pos), 'source': source,
                                    'turn': curr_turn, 'outputs': turn_resps[-2:]})
                #if '</summary>' in full_resp: full_resp = full_resp.replace('</summary>', '</summary>\n\n')
                #if '</file_content>' in full_resp: full_resp = re.sub(r'<file_content>\s*(.*?)\s*</file_content>', r'\n````\n<file_content>\n\1\n</file_content>\n````', full_resp, flags=re.DOTALL)                
                terminal = terminal_from_runner(runner_result, full_resp, effective_max_turns)
                terminal = _normalize_ipc_terminal(task, terminal)
                if source == "ipc" and terminal.get("state") == "failed" and not terminal.get("text"):
                    try:
                        repaired_raw = self._repair_missing_final_answer(task, handler)
                        repaired = _normalize_ipc_terminal(
                            task,
                            {
                                "state": "completed", "text": repaired_raw,
                                "error": "", "question": "", "candidates": [],
                            },
                        )
                        if repaired.get("state") == "completed":
                            terminal = repaired
                    except Exception as repair_error:
                        print(f"[IPC] final_answer_repair_error={format_error(repair_error)}")
                self._publish_terminal_and_settle(
                    task, terminal, handler, display_queue,
                    source=source, turn=curr_turn, outputs=turn_resps,
                    original_query=original_query, generation=generation,
                )
                elapsed_ms = int(max(
                    0.0,
                    (time.time() - self.active_task.get("started_at", time.time())) * 1000,
                ))
                print(
                    "[TURN-PERF] "
                    f"source={source} route={task.get('chat_route', 'quick_chat')} "
                    f"turns={curr_turn} state={terminal.get('state', 'unknown')} "
                    f"elapsed_ms={elapsed_ms}"
                )
                self.history = handler.history_info
            except Exception as e:
                print(f"Backend Error: {format_error(e)}")
                self.last_error = format_error(e)  # store for CLI main block to read
                terminal = {'state': 'failed', 'text': '', 'error': format_error(e),
                            'question': '', 'candidates': []}
                display_queue.put({'done': '', 'terminal': terminal, 'source': source,
                                   'turn': curr_turn, 'outputs': turn_resps.copy()})
            finally:
                if chat_id:
                    discard_key = (chat_id, generation)
                    if self._discard_active_task == discard_key:
                        self._restore_chat_snapshot(self._active_task_snapshot)
                    else:
                        self._save_active_chat_session(chat_id, generation=generation)
                    # 无条件清空：abort 恰在检查点之后到达时会残留标记，
                    # 同 chat 同 generation 的下一任务将被误判回滚。
                    self._discard_active_task = None
                    self._active_task_snapshot = None
                if self.stop_sig: print('User aborted the task.')
                self.is_running = self.stop_sig = False
                self.active_task = None
                self.task_queue.task_done()
                if self.handler is not None: self.handler.code_stop_signal.append(1)

GeneraticAgent = GenericAgent


# ── L4 Session Archiving ─────────────────────────────────────────────

def _archive_session_to_l4(log_path):
    """Archive a single session log to L4. Non-critical — never raise."""
    if not log_path or not os.path.isfile(log_path):
        return
    try:
        sys.path.insert(0, os.path.join(script_dir, 'memory', 'L4_raw_sessions'))
        from compress_session import compress_session, extract_history, format_history_block
        l4_dir = os.path.join(script_dir, 'memory', 'L4_raw_sessions')
        result = compress_session(log_path, l4_dir)
        if result[0] is None:
            return  # too small or no timestamps
        dst, info = result
        history = extract_history(dst)
        if history:
            hist_path = os.path.join(l4_dir, 'all_histories.txt')
            sn = os.path.splitext(os.path.basename(dst))[0]
            with open(hist_path, 'a', encoding='utf-8') as f:
                f.write('\n' + format_history_block(sn, history))
    except Exception:
        pass  # non-critical


# ── Phase 2 Helper Functions ──────────────────────────────────────────

def _write_done_json(task_dir, status, exit_code=0, rounds=0, error=None, started_at=None):
    """Write completion sentinel to task directory. Safe to call with task_dir=None."""
    if not task_dir:
        return
    import datetime
    done = {
        'status': status,
        'exit_code': exit_code,
        'finished_at': datetime.datetime.now().isoformat(),
        'rounds': rounds,
        'error': error,
    }
    if started_at:
        done['started_at'] = started_at
    else:
        pid_file = os.path.join(task_dir, 'pid')
        if os.path.exists(pid_file):
            try:
                done['started_at'] = datetime.datetime.fromtimestamp(
                    os.path.getmtime(pid_file)).isoformat()
            except OSError:
                pass
    with open(os.path.join(task_dir, 'done.json'), 'w', encoding='utf-8') as f:
        json.dump(done, f, ensure_ascii=False, indent=2)


def _check_stale_pid_readonly(pid_file):
    """
    Check if PID file points to a live agentmain process (read-only).
    Cross-platform: Linux /proc/<pid>/cmdline, Windows wmic.
    """
    try:
        with open(pid_file) as f:
            pid_str = f.read().strip()
        if not pid_str.isdigit():
            os.remove(pid_file)
            return
        pid = int(pid_str)
        cmdline = _read_pid_cmdline(pid)
        if cmdline is None:
            os.remove(pid_file)
            return
        if 'agentmain.py' not in cmdline:
            os.remove(pid_file)
            return
        print(f'Error: Task already running (PID {pid}); pidfile={pid_file}')
        print(f'  cmdline: {cmdline[:200]}')
        sys.exit(1)
    except (IOError, OSError, ValueError) as e:
        print(f'Warning: could not verify PID file {pid_file}: {e}')
        print('Proceeding with spawn (stale PID file removed).')
        try:
            os.remove(pid_file)
        except OSError:
            pass


def _read_pid_cmdline(pid):
    """Read command line of a PID. Cross-platform, no os.kill."""
    if os.name == 'nt':
        return _read_pid_cmdline_windows(pid)
    return _read_pid_cmdline_linux(pid)


def _read_pid_cmdline_linux(pid):
    """Linux: /proc/<pid>/cmdline."""
    cmdline_path = f'/proc/{pid}/cmdline'
    if not os.path.exists(cmdline_path):
        return None
    with open(cmdline_path, 'rb') as f:
        raw = f.read()
    return raw.replace(b'\x00', b' ').decode('utf-8', errors='replace')


def _read_pid_cmdline_windows(pid):
    """Windows: wmic."""
    import subprocess
    try:
        result = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/format:value"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000
        )
        for line in result.stdout.splitlines():
            if line.startswith("CommandLine="):
                return line[len("CommandLine="):].strip()
        return None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


if __name__ == '__main__':
    import argparse
    from datetime import datetime
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', metavar='IODIR', help='一次性任务模式(文件IO)')
    parser.add_argument('--reflect', metavar='SCRIPT', help='反射模式：加载监控脚本，check()触发时发任务')
    parser.add_argument('--input', help='prompt')
    parser.add_argument('--llm_no', type=int, default=0)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--nobg', action='store_true')
    parser.add_argument('--once', action='store_true', help='Phase 2: exit after one round, write done.json')
    args, _unknown = parser.parse_known_args()
    _reflect_args = dict(zip([k.lstrip('-') for k in _unknown[::2]], _unknown[1::2])) if _unknown else {}

    # ── Launch-time config check (non-blocking, warn only) ──
    if not args.task and not args.reflect:
        try:
            import importlib.util
            cfg_path = os.path.join(script_dir, 'config_check.py')
            if os.path.isfile(cfg_path):
                spec = importlib.util.spec_from_file_location('config_check', cfg_path)
                cc = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cc)
                results = cc.run_all()
                fails = sum(1 for _, _, s, _ in results if s == 'fail')
                warns = sum(1 for _, _, s, _ in results if s == 'warn')
                if fails > 0:
                    print(f'⚠️  Config check: {fails} failures, {warns} warnings. Run python config_check.py for details.')
                elif warns > 0:
                    print(f'ℹ️  Config check: {warns} warnings. Run python config_check.py for details.')
        except Exception:
            pass  # non-critical

    if args.task and not args.nobg:
        import subprocess, platform
        d = resolve_task_dir(args.task, script_dir)
        os.makedirs(d, exist_ok=True)
        # Duplicate spawn guard via /proc/<pid>/cmdline (read-only, no os.kill)
        pid_file = os.path.join(d, 'pid')
        if os.path.exists(pid_file):
            _check_stale_pid_readonly(pid_file)
        cmd = [sys.executable, os.path.abspath(__file__)] + [a for a in sys.argv[1:]] + ['--nobg']
        # NOTE: Do NOT auto-append --once. User must pass --once explicitly.
        popen_kwargs = dict(cwd=script_dir,
            stdout=open(os.path.join(d, 'stdout.log'), 'w', encoding='utf-8'),
            stderr=open(os.path.join(d, 'stderr.log'), 'w', encoding='utf-8'))
        if platform.system() != 'Windows':
            popen_kwargs['start_new_session'] = True
        else:
            popen_kwargs['creationflags'] = 0x08000000
        p = subprocess.Popen(cmd, **popen_kwargs)
        with open(os.path.join(d, 'pid'), 'w') as f:
            f.write(str(p.pid))
        print('PID:', p.pid)
        sys.exit(0)

    runtime_policy = runtime_service_policy(task_mode=bool(args.task))
    agent = GeneraticAgent(start_watchdog=runtime_policy["watchdog"])
    agent.next_llm(args.llm_no)
    agent.verbose = args.verbose
    threading.Thread(target=agent.run, daemon=True).start()

    # ── IPC server for frontend communication ──
    if runtime_policy["ipc"]:
        try:
            from frontends.shared.ipc_server import IpcServer
            IpcServer(agent).start()
        except Exception as e:
            print(f"[IPC] server start skipped: {e}")

    if args.task:
        agent.peer_hint = False
        agent.force_non_stream = True
        agent.task_dir = d = resolve_task_dir(args.task, script_dir)
        _started_at = datetime.now().isoformat()
        nround = ''
        infile = os.path.join(d, 'input.txt')

        # ── Path D: input.txt 缺失 ──
        if not args.input and not os.path.exists(infile):
            err = f'input.txt not found at {infile}. Provide --input or create input.txt manually.'
            print(err)
            _write_done_json(d, 'error', exit_code=1, error=err, started_at=_started_at)
            sys.exit(1)

        if args.input:
            os.makedirs(d, exist_ok=True)
            import glob; [os.remove(f) for f in glob.glob(os.path.join(d, 'output*.txt'))]
            with open(infile, 'w', encoding='utf-8') as f: f.write(args.input)
        if (fh := consume_file(d, '_history.json')): agent.llmclient.backend.history = json.loads(fh)
        with open(infile, encoding='utf-8') as f: raw = f.read()

        try:
            while True:
                dq = agent.put_task(raw, source='task')
                while 'done' not in (item := dq.get(timeout=1200)):
                    if 'next' in item and random.random() < 0.95:
                        with open(f'{d}/output{nround}.txt', 'w', encoding='utf-8') as f: f.write(item.get('next', ''))

                # ── Path B: LLM 异常 ──
                if agent.last_error:
                    with open(f'{d}/output{nround}.txt', 'w', encoding='utf-8') as f:
                        f.write(item['done'] + '\n\n[ROUND END]\n')
                    _write_done_json(d, 'error', exit_code=1, rounds=item.get('turn', 1),
                                     error=agent.last_error, started_at=_started_at)
                    _archive_session_to_l4(agent.log_path)
                    break

                with open(f'{d}/output{nround}.txt', 'w', encoding='utf-8') as f:
                    f.write(item['done'] + '\n\n[ROUND END]\n')
                consume_file(d, '_stop')

                # ── Path A: --once 模式（跳过 reply.txt 等待）──
                if args.once:
                    actual_turns = item.get('turn', 1)
                    terminal = item.get('terminal') or {
                        'state': 'completed', 'text': item.get('done', ''), 'error': ''
                    }
                    state = terminal.get('state', 'failed')
                    if state == 'completed':
                        _write_done_json(d, 'completed', exit_code=0, rounds=actual_turns,
                                         started_at=_started_at)
                    elif state == 'needs_input':
                        _write_done_json(d, 'needs_input', exit_code=2, rounds=actual_turns,
                                         error=terminal.get('question') or '需要用户输入',
                                         started_at=_started_at)
                    else:
                        _write_done_json(d, 'error', exit_code=1, rounds=actual_turns,
                                         error=terminal.get('error') or state,
                                         started_at=_started_at)
                    _archive_session_to_l4(agent.log_path)
                    break

                # ── Path C: reply.txt 等待（传统交互模式）──
                for _ in range(300):
                    time.sleep(2)
                    if (raw := consume_file(d, 'reply.txt')): break
                else:
                    _write_done_json(d, 'completed', exit_code=0, rounds=item.get('turn', 1),
                                     error='reply.txt timeout — no reply within 10 min',
                                     started_at=_started_at)
                    break
                nround = nround + 1 if isinstance(nround, int) else 1
        except Exception as e:
            _write_done_json(d, 'error', exit_code=1, error=format_error(e), started_at=_started_at)
            raise
    elif args.reflect:
        agent.peer_hint = False
        agent.force_non_stream = True
        import importlib.util
        spec = importlib.util.spec_from_file_location('reflect_script', args.reflect)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        if hasattr(mod, 'init'): mod.init(_reflect_args)
        _mt = os.path.getmtime(args.reflect)
        print(f'[Reflect] loaded {args.reflect}' + (f' args={_reflect_args}' if _reflect_args else ''))
        while True:
            if os.path.getmtime(args.reflect) != _mt:
                try:
                    spec.loader.exec_module(mod); _mt = os.path.getmtime(args.reflect)
                    if hasattr(mod, 'init'): mod.init(_reflect_args)
                    print('[Reflect] reloaded')
                except Exception as e: print(f'[Reflect] reload error: {e}')
            try: task = mod.check()
            except Exception as e: 
                print(f'[Reflect] check() error: {e}'); task = None
            if task and task == '/exit': break
            if not task:
                time.sleep(getattr(mod, 'INTERVAL', 5)); continue
            print(f'[Reflect] triggered: {task[:80]}')
            dq = agent.put_task(task, source='reflect')
            try:
                while 'done' not in (item := dq.get(timeout=1200)): pass
                result = item['done']
                print(result)
            except Exception as e:
                if getattr(mod, 'ONCE', False): raise
                print(f'[Reflect] drain error: {e}'); result = f'[ERROR] {e}'
            _archive_session_to_l4(agent.log_path)
            log_dir = os.path.join(script_dir, 'temp/reflect_logs'); os.makedirs(log_dir, exist_ok=True)
            script_name = os.path.splitext(os.path.basename(args.reflect))[0]
            open(os.path.join(log_dir, f'{script_name}_{datetime.now():%Y-%m-%d}.log'), 'a', encoding='utf-8').write(f'[{datetime.now():%m-%d %H:%M}]\n{result}\n\n')
            if (on_done := getattr(mod, 'on_done', None)):
                try: on_done(result)
                except Exception as e: print(f'[Reflect] on_done error: {e}')
            if getattr(mod, 'ONCE', False): print('[Reflect] ONCE=True, exiting.'); break
    else:
        try: import readline
        except Exception: pass
        agent.inc_out = True
        if sys.stdout.isatty():
            try: model = agent.get_llm_name(model=True) or '?'
            except Exception: model = '?'
            try:
                sys.stdout.write(f'\x1b[92m✦\x1b[0m \x1b[1mGenericAgent\x1b[0m '
                                 f'\x1b[90m· cli · model:\x1b[0m {model}\n')
                sys.stdout.flush()
            except Exception: pass
        while True:
            q = input('> ').strip()
            if not q: continue
            try:
                dq = agent.put_task(q, source='user')
                while True:
                    item = dq.get()
                    if 'next' in item: print(item['next'], end='', flush=True)
                    if 'done' in item: print(); break
            except KeyboardInterrupt:
                agent.abort()
                print('\n[Interrupted]')
