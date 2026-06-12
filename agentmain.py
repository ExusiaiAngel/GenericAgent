import os, sys, threading, queue, time, json, re, random, locale
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
    TS = open(os.path.join(script_dir, f'assets/tools_schema{suffix}.json'), 'r', encoding='utf-8').read()
    TOOLS_SCHEMA = json.loads(TS if os.name == 'nt' else TS.replace('powershell', 'bash'))
load_tool_schema()

lang_suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
mem_dir = os.path.join(script_dir, 'memory')
if not os.path.exists(mem_dir): os.makedirs(mem_dir)
mem_txt = os.path.join(mem_dir, 'global_mem.txt')
if not os.path.exists(mem_txt): open(mem_txt, 'w', encoding='utf-8').write('# [Global Memory - L2]\n')
mem_insight = os.path.join(mem_dir, 'global_mem_insight.txt')
if not os.path.exists(mem_insight):
    t = os.path.join(script_dir, f'assets/global_mem_insight_template{lang_suffix}.txt')
    open(mem_insight, 'w', encoding='utf-8').write(open(t, encoding='utf-8').read() if os.path.exists(t) else '')
cdp_cfg = os.path.join(script_dir, 'assets/tmwd_cdp_bridge/config.js')
if not os.path.exists(cdp_cfg):
    try:
        os.makedirs(os.path.dirname(cdp_cfg), exist_ok=True)
        open(cdp_cfg, 'w', encoding='utf-8').write(f"const TID = '__ljq_{hex(random.randint(0, 99999999))[2:8]}';")
    except Exception as e: print(f'[WARN] CDP config init failed: {e} — advanced web features (tmwebdriver) will be unavailable.')

def get_system_prompt():
    with open(os.path.join(script_dir, f'assets/sys_prompt{lang_suffix}.txt'), 'r', encoding='utf-8') as f: prompt = f.read()
    prompt += f"\nToday: {time.strftime('%Y-%m-%d %a')}\n"
    prompt += get_global_memory()
    return prompt

class GenericAgent:
    def __init__(self):
        os.makedirs(os.path.join(script_dir, 'temp'), exist_ok=True)
        self.lock = threading.Lock()
        self.task_dir = None
        self.history = []; self.handler = None; 
        self.task_queue = queue.Queue() 
        self.is_running = False; self.stop_sig = False; self.llm_no = 0;  
        self.inc_out = False; self.verbose = True; self.show_mode = 'text'
        self.peer_hint = True
        self.force_non_stream = False
        logid = f'{(time.time_ns() + random.randrange(1_000_000)) % 1_000_000:06d}'
        self.log_path = os.path.join(script_dir, f'temp/model_responses/model_responses_{logid}.txt')
        self.load_llm_sessions()
        self.last_error = None  # set by run() on LLM exception

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

    def abort(self):
        if not self.is_running: return
        print('Abort current task...')
        self.stop_sig = True
        if self.handler is not None: self.handler.code_stop_signal.append(1)
            
    def put_task(self, query, source="user", images=None):
        display_queue = queue.Queue()
        self.task_queue.put({"query": query, "source": source, "images": images or [], "output": display_queue})
        return display_queue

    # i know it is dangerous, but raw_query is dangerous enough it doesn't enlarge
    def _handle_slash_cmd(self, raw_query, display_queue):
        if not raw_query.startswith('/'): return raw_query
        if _sm := re.match(r'/session\.(\w+)=(.*)', raw_query.strip()):
            k, v = _sm.group(1), _sm.group(2)
            vfile = os.path.abspath(os.path.normpath(os.path.join(script_dir, 'temp', v)))
            temp_dir = os.path.abspath(os.path.join(script_dir, 'temp'))
            if os.path.isfile(vfile) and vfile.startswith(temp_dir + os.sep):
                v = open(vfile, encoding='utf-8').read().strip()
            try: v = json.loads(v)  # cover number parsing
            except (json.JSONDecodeError, ValueError): pass
            setattr(self.llmclient.backend, k, v)
            display_queue.put({'done': smart_format(f"✅ session.{k} = {repr(v)}", max_str_len=500), 'source': 'system'})
            return None
        if raw_query.strip() == '/resume':
            return r'帮我看看最近有哪些会话可以恢复。读model_responses/目录，按修改时间取最近10个文件，从每个文件里找最后一个<history>...</history>块，用一句话总结每个会话在聊什么，列表给我选。注意读文件后要把字面的\n替换成真换行才能正确匹配。'
        return raw_query

    def run(self):
        while True:
            task = self.task_queue.get()
            if isinstance(task, str): break
            raw_query, source, display_queue = task["query"], task["source"], task["output"]
            raw_query = self._handle_slash_cmd(raw_query, display_queue)
            if raw_query is None:
                self.task_queue.task_done(); continue
            self.is_running = True
            if len(raw_query) > 1500:
                task_file = os.path.join(script_dir, 'temp', f'user_prompt_{int(time.time())}.md')
                with open(task_file, 'w', encoding='utf-8') as f: f.write(raw_query)
                raw_query = f'Long user prompt saved to {task_file}. Read and execute.'
            rquery = smart_format(raw_query.replace('\n', ' '), max_str_len=200)
            self.history.append(f"[USER]: {rquery}")
            
            sys_prompt = get_system_prompt() + getattr(self.llmclient.backend, 'extra_sys_prompt', '')
            if self.peer_hint: sys_prompt += f"\n[Peer] 用户提及其他会话/后台任务状态时: temp/model_responses/ (只找近期修改的文件尾部)\n"
            handler = GenericAgentHandler(self, self.history, os.path.join(script_dir, 'temp'))
            if getattr(self, 'no_print', False): handler.print = lambda *a, **k: None
            if self.handler and 'key_info' in self.handler.working: 
                ki = re.sub(r'\n\[SYSTEM\] 此为.*?工作记忆[。\n]*', '', self.handler.working['key_info'])  # 去旧
                handler.working['key_info'] = ki
                handler.working['passed_sessions'] = ps = self.handler.working.get('passed_sessions', 0) + 1
                if ps > 0: handler.working['key_info'] += f'\n[SYSTEM] 此为 {ps} 个对话前设置的key_info，若已在新任务，先更新或清除工作记忆。\n'
            self.handler = handler  # although new handler, the **full** history is in llmclient, so it is full history!
            self.llmclient.log_path = self.log_path
            if self.force_non_stream:
                self.llmclient.backend.stream = False
                self.llmclient.backend.read_timeout = max(self.llmclient.backend.read_timeout, 1200)
            gen = agent_runner_loop(self.llmclient, sys_prompt, raw_query, handler, TOOLS_SCHEMA, 
                                    max_turns=80, verbose=self.verbose, yield_info=True)
            try:
                full_resp = ""; last_pos = 0; curr_turn = 0; turn_resps = []
                for chunk in gen:
                    if consume_file(self.task_dir, '_stop'): self.abort() 
                    if self.stop_sig: break
                    if isinstance(chunk, dict) and 'turn' in chunk: 
                        curr_turn = chunk['turn']; turn_resps.append(''); continue
                    full_resp += chunk;  turn_resps[-1] += chunk
                    if len(full_resp) - last_pos > 30 or 'LLM Running' in chunk:
                        display_queue.put({'next': full_resp[last_pos:] if self.inc_out else full_resp, 
                                           'source': source, 'turn': curr_turn, 'outputs': turn_resps[-2:]})
                        last_pos = len(full_resp)
                if self.inc_out and last_pos < len(full_resp):
                    display_queue.put({'next': full_resp[last_pos:], 'source': source,
                                    'turn': curr_turn, 'outputs': turn_resps[-2:]})
                #if '</summary>' in full_resp: full_resp = full_resp.replace('</summary>', '</summary>\n\n')
                #if '</file_content>' in full_resp: full_resp = re.sub(r'<file_content>\s*(.*?)\s*</file_content>', r'\n````\n<file_content>\n\1\n</file_content>\n````', full_resp, flags=re.DOTALL)                
                display_queue.put({'done': full_resp, 'source': source, 'turn': curr_turn, 'outputs': turn_resps.copy()})
                self.history = handler.history_info
            except Exception as e:
                print(f"Backend Error: {format_error(e)}")
                self.last_error = format_error(e)  # store for CLI main block to read
                display_queue.put({'done': full_resp + f'\n```\n{format_error(e)}\n```', 'source': source, 'turn': curr_turn, 'outputs': turn_resps.copy()})
            finally:
                if self.stop_sig: print('User aborted the task.')
                self.is_running = self.stop_sig = False
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

    agent = GeneraticAgent()
    agent.next_llm(args.llm_no)
    agent.verbose = args.verbose
    threading.Thread(target=agent.run, daemon=True).start()

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
                    _write_done_json(d, 'completed', exit_code=0, rounds=actual_turns,
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
