"""子代理系统 — GenericAgent 并行任务执行。

子代理在独立线程中运行，拥有独立的 LLM client 和上下文。
主 Agent 通过工具 spawn / list / talk 与子代理交互。

设计原则：
- threading.Thread（非 multiprocessing），因 LLM 调用是 I/O 密集型
- 每个子代理拥有独立 ToolClient
- 状态通过共享 dict + threading.Lock 同步
"""

import json, os, sys, time, threading, traceback as _tb
from datetime import datetime
from typing import Optional

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

_SUBAGENT_DIR = os.path.join(_PROJ_ROOT, "temp", "sub_agents")
os.makedirs(_SUBAGENT_DIR, exist_ok=True)

_sub_agents: dict[str, dict] = {}
_sub_lock = threading.Lock()
_next_id = 0
_config = {"auto_cleanup": True}

_REGISTRY_FILE = os.path.join(_SUBAGENT_DIR, "_registry.json")


def _save_registry():
    """Persist sub-agent registry to disk for crash recovery."""
    try:
        with _sub_lock:
            data = {}
            for sid, info in _sub_agents.items():
                alive = info["thread"].is_alive() if info["thread"] else False
                st = info.get("status_ref", {})
                data[sid] = {
                    "task": info["task"],
                    "created_at": info["created_at"],
                    "max_turns": info["max_turns"],
                    "alive": alive,
                    "status": st.get("status", "unknown"),
                    "progress": (st.get("progress") or "")[:200],
                    "turns": st.get("turns", 0),
                    "work_dir": info["work_dir"],
                }
        with open(_REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_registry():
    """Load sub-agent registry from disk (after restart). Non-recoverable threads are marked."""
    try:
        if not os.path.isfile(_REGISTRY_FILE):
            return {}
        with open(_REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Mark all as 'lost' since threads don't survive restart
        for sid, info in data.items():
            info["alive"] = False
            info["status"] = "lost_after_restart"
        return data
    except Exception:
        return {}


def _new_id() -> str:
    global _next_id
    _next_id += 1
    return f"sub_{int(time.time())}_{_next_id}"


def spawn(task_desc: str, max_turns: int = 30) -> dict:
    """创建一个子代理线程。"""
    sid = _new_id()
    work_dir = os.path.join(_SUBAGENT_DIR, sid)
    os.makedirs(work_dir, exist_ok=True)

    # 初始化状态
    status = {
        "id": sid, "task": task_desc, "status": "starting",
        "progress": "等待线程启动...",
        "created_at": datetime.now().isoformat(),
        "result": "", "turns": 0, "pid": os.getpid(),
    }
    _write_status(work_dir, status)

    info = {
        "task": task_desc, "status": "starting", "status_ref": status,
        "thread": None, "work_dir": work_dir, "created_at": time.time(),
        "max_turns": max_turns,
    }

    # 在独立线程中启动子代理
    t = threading.Thread(
        target=_sub_agent_main,
        args=(sid, task_desc, work_dir, max_turns, info),
        name=f"sub-{sid}",
        daemon=True,
    )
    info["thread"] = t
    t.start()

    with _sub_lock:
        _sub_agents[sid] = info

    _save_registry()
    return {"ok": True, "id": sid, "reason": f"子代理 {sid} 已启动"}


def list_subagents() -> list[dict]:
    """列出所有子代理状态（含重启后丢失的）。"""
    results = []
    with _sub_lock:
        # Include lost sub-agents from registry (inside lock to avoid TOCTOU with spawn)
        if not _sub_agents:
            lost = _load_registry()
            for sid, info in lost.items():
                results.append({
                    "id": sid,
                    "task": (info.get("task", "") or "")[:100],
                    "status": info.get("status", "lost_after_restart"),
                    "alive": False,
                    "created_at": info.get("created_at", ""),
                    "progress": info.get("progress", ""),
                    "turns": info.get("turns", 0),
                    "result_preview": "[lost after restart]",
                })
        for sid, info in dict(_sub_agents).items():
            alive = info["thread"].is_alive() if info["thread"] else False
            st = info.get("status_ref", {})
            results.append({
                "id": sid,
                "task": (info["task"] or "")[:100],
                "status": st.get("status", "unknown") if alive else st.get("status", "ended"),
                "alive": alive,
                "created_at": st.get("created_at", ""),
                "progress": (st.get("progress") or "")[:200],
                "turns": st.get("turns", 0),
                "result_preview": (st.get("result") or "")[:200],
            })
    return results


def talk(sid: str, message: str) -> dict:
    """向运行中的子代理发送消息（文件注入）。"""
    with _sub_lock:
        info = _sub_agents.get(sid)
    if not info:
        return {"ok": False, "reason": f"子代理 {sid} 不存在"}
    if not info["thread"].is_alive():
        return {"ok": False, "reason": f"子代理 {sid} 已结束"}
    inject_file = os.path.join(info["work_dir"], "_inject.txt")
    with open(inject_file, "a", encoding="utf-8") as f:
        f.write(f"\n[MASTER] {message}\n")
    return {"ok": True, "reason": f"消息已发送给 {sid}"}


def collect(sid: str) -> dict:
    """收集已结束的子代理结果。"""
    with _sub_lock:
        info = _sub_agents.pop(sid, None)
    if not info:
        return {"ok": False, "reason": f"子代理 {sid} 不存在"}
    if info["thread"].is_alive():
        with _sub_lock:
            _sub_agents[sid] = info
        return {"ok": False, "reason": f"子代理 {sid} 仍在运行"}
    st = info.get("status_ref", {})
    _save_registry()
    return {
        "ok": True, "id": sid,
        "status": st.get("status", "ended"),
        "result": st.get("result", ""),
        "turns": st.get("turns", 0),
        "cleaned": True,
    }


def cleanup_all():
    """清理已结束的子代理记录。"""
    with _sub_lock:
        for sid in list(_sub_agents.keys()):
            info = _sub_agents[sid]
            if not info["thread"].is_alive():
                del _sub_agents[sid]


# ── 子代理线程主函数 ──────────────────────────────────────────────

def _sub_agent_main(sid: str, task_desc: str, work_dir: str,
                    max_turns: int, info: dict):
    """在子线程中运行的 agent 主函数。"""
    log_path = os.path.join(work_dir, "agent.log")
    log_file = open(log_path, "w", encoding="utf-8", buffering=1)

    def log(msg):
        line = f"[{datetime.now().isoformat()}] {msg}"
        log_file.write(line + "\n")
        log_file.flush()

    def update_status(**kw):
        with _sub_lock:
            st = info.setdefault("status_ref", {})
            st.update(kw)
        _write_status(work_dir, st)

    try:
        log("Sub-agent starting...")

        # 1. 加载工具 schema（模块级已加载）
        from agentmain import TOOLS_SCHEMA, get_system_prompt, script_dir

        # 2. 创建独立 LLM client
        from llmcore import ToolClient, resolve_client
        from llmcore import reload_mykeys
        mykeys, _ = reload_mykeys()

        llm_client = None
        for k in mykeys:
            if any(x in k for x in ["api", "config", "cookie"]):
                try:
                    c = resolve_client(k)
                    if c and hasattr(c, 'chat'):
                        llm_client = c
                        break
                except Exception as e:
                    log(f"resolve_client({k}) failed: {e}")
        if llm_client is None:
            raise RuntimeError("无法创建 LLM client")

        log(f"LLM client: {llm_client.name}")
        update_status(progress=f"LLM: {llm_client.name}", status="running")

        # 3. 创建 Handler
        from ga import GenericAgentHandler

        class _NullParent:
            verbose = False
            task_dir = None
            inc_out = False
            no_print = True
            handler = None
            _turn_end_hooks = {}
            is_running = False
            stop_sig = False

        parent = _NullParent()
        handler = GenericAgentHandler(parent, [], work_dir)
        handler.print = lambda *a, **k: None

        # 4. 系统提示词
        sys_prompt = get_system_prompt()
        sys_prompt += (
            "\n\n你是一个 GenericAgent 的子代理(sub-agent)，专注于执行以下任务。\n"
            "请独立完成任务，完成后在回复末尾输出 <sub_done>任务摘要</sub_done>。\n"
            "使用所有可用工具。注意：工作目录是 temp/，可创建文件。"
        )

        # 5. 消息历史
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"请执行以下任务：\n\n{task_desc}\n\n"
                                        f"独立完成，使用工具。完成后输出 <sub_done>摘要</sub_done>"},
        ]

        full_response = ""
        turn = 0
        from agent_loop import exhaust, _clean_content

        while turn < max_turns:
            turn += 1
            log(f"Turn {turn}/{max_turns}")
            update_status(progress=f"第 {turn}/{max_turns} 轮思考中...", turns=turn)

            # 检查注入消息
            inject_file = os.path.join(work_dir, "_inject.txt")
            if os.path.exists(inject_file):
                try:
                    with open(inject_file, "r", encoding="utf-8") as f:
                        inject_data = f.read().strip()
                    if inject_data:
                        messages.append({
                            "role": "user",
                            "content": f"[MASTER 消息]\n{inject_data}"
                        })
                        log(f"Injected master message: {inject_data[:100]}")
                    os.remove(inject_file)
                except Exception:
                    pass

            # LLM 调用
            try:
                response_gen = llm_client.chat(messages=messages, tools=TOOLS_SCHEMA)
                response = exhaust(response_gen)
            except Exception as e:
                log(f"LLM chat error: {e}")
                update_status(progress=f"LLM 错误: {e}", status="error")
                break

            content = ""
            if hasattr(response, 'content') and response.content:
                content = response.content
            elif hasattr(response, 'text') and response.text:
                content = response.text
            elif isinstance(response, str):
                content = response
            else:
                content = str(response)

            full_response += content + "\n"
            cleaned = _clean_content(content) if content else ""
            if cleaned:
                log(f"Response: {cleaned[:100]}")

            # 检查完成信号
            if "<sub_done>" in content:
                done_match = ""
                try:
                    done_match = content.split("<sub_done>")[1].split("</sub_done>")[0].strip()
                except IndexError:
                    done_match = cleaned[:300]
                log(f"Task done: {done_match[:200]}")
                update_status(
                    progress="任务完成",
                    status="done",
                    result=done_match or cleaned[:500],
                    turns=turn,
                )
                return

            # 解析工具调用
            tool_calls = []
            if hasattr(response, 'tool_calls') and response.tool_calls:
                import json as _json
                for tc in response.tool_calls:
                    try:
                        tool_calls.append({
                            'tool_name': tc.function.name,
                            'args': _json.loads(tc.function.arguments),
                            'id': tc.id,
                        })
                    except Exception as e:
                        log(f"parse tool call error: {e}")
            else:
                tool_calls = [{'tool_name': 'no_tool', 'args': {}}]

            # 执行工具
            tool_results = []
            next_prompts = set()
            exit_early = False

            for ii, tc in enumerate(tool_calls):
                tool_name = tc['tool_name']
                args = tc.get('args', {})

                if tool_name == 'no_tool':
                    if cleaned:
                        # 有文本回复但没有工具调用 - 直接回复用户
                        pass
                    continue

                update_status(progress=f"[{turn}] 🛠️ {tool_name}", turns=turn)
                log(f"Tool: {tool_name} args={str(args)[:200]}")

                try:
                    gen = handler.dispatch(tool_name, args, response,
                                           index=ii, tool_num=len(tool_calls))
                    try:
                        v = next(gen)
                        def proxy():
                            yield v
                            return (yield from gen)
                        from agent_loop import exhaust as _exh
                        outcome = _exh(proxy())
                    except StopIteration as _e:
                        outcome = _e.value

                    if outcome:
                        if getattr(outcome, 'should_exit', False):
                            exit_early = True
                            break
                        if outcome.next_prompt:
                            data_str = str(outcome.data or "")[:300]
                            tool_results.append({
                                'tool_use_id': tc.get('id', ''),
                                'content': data_str,
                            })
                            next_prompts.add(outcome.next_prompt)
                            log(f"{tool_name} → {data_str[:100]}")
                except Exception as e:
                    log(f"dispatch {tool_name} error: {e}")
                    next_prompts.add(f"工具 {tool_name} 执行出错: {e}")

            if exit_early:
                break

            if not next_prompts and not tool_calls:
                # 纯文本回复，没有工具调用 - 任务可能自然完成了
                update_status(
                    progress="子代理已回复（无工具调用）",
                    status="done",
                    result=cleaned[:500],
                    turns=turn,
                )
                return
            elif not next_prompts:
                break

            # 准备下一轮
            next_prompt = "\n".join(next_prompts)
            if tool_results:
                messages.append({
                    "role": "user",
                    "content": next_prompt,
                    "tool_results": tool_results,
                })
            else:
                messages.append({"role": "user", "content": next_prompt})

        # 达到最大轮次
        update_status(
            progress=f"达到最大轮次 ({max_turns})",
            status="timeout",
            result=full_response[:1000],
            turns=turn,
        )
        log(f"Max turns ({max_turns}) reached.")

    except Exception as e:
        _tb.print_exc(file=log_file)
        log(f"FATAL: {e}")
        update_status(progress=f"异常: {e}", status="error", result=_tb.format_exc())

    finally:
        # 保存完整回复到文件供调用方读取 (NapCat bot 子代理方案)
        try:
            full_resp_path = os.path.join(work_dir, "full_response.txt")
            with open(full_resp_path, "w", encoding="utf-8") as f:
                f.write(full_response)
        except Exception:
            pass
        log_file.close()


# ── 状态文件工具 ──────────────────────────────────────────────────

def _write_status(work_dir: str, data: dict):
    """写入 JSON 状态文件（供外部监控用）。Atomic write via temp file + rename."""
    try:
        path = os.path.join(work_dir, "status.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        print(f"[SubAgent] _write_status error: {e}")
    except Exception:
        pass


def _read_status(work_dir: str) -> Optional[dict]:
    try:
        path = os.path.join(work_dir, "status.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None


def configure(**kwargs):
    _config.update(kwargs)
