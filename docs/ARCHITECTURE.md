# GenericAgent — Technical Architecture

**Version**: 0.1.0 | **Python**: 3.10–3.13 | **OS**: Windows 11 / Linux / WSL  
**Last updated**: 2026-06-11

---

## 1. Overview

GenericAgent is a self-evolving autonomous agent framework built around six core subsystems:

1. **Agent Loop** — Turn-based execution with generator-based tool dispatch
2. **LLM Core** — Multi-provider session management with streaming chat
3. **Tool Layer** — 11 tools via OpenAI-compatible function-calling schema
4. **Memory System** — L0-L4 layered persistence with automated mining
5. **Autonomy Modules** — Scheduler, goal system, proactive monitoring
6. **Frontends** — TUI, Streamlit, Qt, IM bots, desktop pet

```
                  ┌──────────────────────┐
                  │    Frontends (7)      │
                  │ TUI Qt Streamlit Chat │
                  └──────────┬───────────┘
                             │
                  ┌──────────▼───────────┐
                  │    agentmain.py       │
                  │  CLI / Task / Reflect │
                  └──────────┬───────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
   ┌──────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
   │ agent_loop  │   │   llmcore   │   │    ga.py    │
   │ (130 lines) │   │ (1063 lines)│   │ (700+ lines)│
   │ Turn runner │   │ LLM sessions│   │ 11 tools    │
   └─────────────┘   └─────────────┘   └─────────────┘
                             │
                  ┌──────────▼───────────┐
                  │   Memory System       │
                  │  L0 → L1 → L2 → L3   │
                  │        ↕              │
                  │   L4 (auto-mined)     │
                  └──────────────────────┘
```

---

## 2. Agent Loop (`agent_loop.py`)

The core execution engine — 130 lines.

### Design
- **Turn-based**: System prompt → user input → LLM response → tool calls → results → next prompt → repeat
- **Generator-driven**: Both the main loop and individual tools are Python generators, enabling streaming output and cooperative cancellation
- **Convention dispatch**: Tools are dispatched by `do_{tool_name}` method on the handler — no registry, no wiring

### Key Components

```python
@dataclass
class StepOutcome:
    data: Any                              # Tool return value
    next_prompt: Optional[str] = None       # Next turn's user message
    should_exit: bool = False               # True = end agent loop immediately

class BaseHandler:
    def dispatch(self, tool_name, args, response):
        method_name = f"do_{tool_name}"     # Convention: do_web_search → web_search
        if hasattr(self, method_name):
            ret = yield from getattr(self, method_name)(args, response)
            return ret
```

### Execution Flow
```
agent_runner_loop()
  ├── for turn in range(max_turns):
  │   ├── client.chat(messages, tools_schema)    # LLM call
  │   ├── parse tool_calls from response
  │   ├── for each tool_call:
  │   │   ├── handler.dispatch(tool_name, args)   # Generator yield
  │   │   └── collect StepOutcome
  │   ├── handler.turn_end_callback()             # History + injection
  │   └── if should_exit: break
  └── return exit_reason
```

### Safety Features
- **Max turns** (default 80): prevents infinite loops
- **Turn-based warnings**: at 7 turns (warn), 75 turns (force ask_user)
- **Stop signal**: `handler.code_stop_signal` list — any tool can abort
- **Plan mode detection**: Intercepts premature completion claims when in plan mode

---

## 3. LLM Core (`llmcore.py`)

1063 lines of multi-provider LLM session management.

### Provider Support
- **DeepSeek** (primary) — OpenAI-compatible API at `api.deepseek.com/v1`
- **Claude** (native) — Anthropic Messages API
- **OpenAI** (native) — OpenAI Chat Completions API
- **Mixin sessions** — Route between multiple providers dynamically
- **Zhipu, MiniMax, Moonshot, OpenRouter** — via OpenAI-compatible adapters

### Key Features
- **Streaming chat** with chunk-by-chunk yield (generator-based)
- **Tool schema injection** — Tools passed as OpenAI-compatible function definitions
- **Multi-key rotation** — `reload_mykeys()` scans `mykey.py` for multiple API keys
- **Session persistence** — History maintained across model switches
- **Proxy support** — `GENERICAGENT_PROXY` env var for all HTTP calls
- **Retry logic** — Automatic retry on transient errors with exponential backoff

### Configuration (`mykey.py`)
```python
# Example: DeepSeek
DEEPSEEK_API_KEY = 'sk-...'           # Env var or mykey.py
GENERICAGENT_PROXY = 'http://...'     # Optional HTTP proxy

# Multi-model: add named entries for rotation
native_claude_1 = {'apikey': 'sk-ant-...'}
oai_gpt4 = {'apikey': 'sk-...', 'base_url': 'https://api.openai.com/v1'}
```

---

## 4. Tool Layer (`ga.py`)

700+ lines implementing all 11 agent tools.

### Tool Dispatch
```
LLM response.tool_calls:
  {name: "web_search", args: {query: "Python 3.14"}}
       │
       ▼
BaseHandler.dispatch("web_search", args)
       │
       ▼
GenericAgentHandler.do_web_search(args, response)
       │
       ▼
web_search(query)  →  {status: "success", results: [...]}
```

### Tool Inventory

| # | Tool | Type | Description |
|:--|------|------|-------------|
| 1 | `code_run` | Execution | Python or PowerShell in isolated temp files; streaming stdout; timeout + kill |
| 2 | `file_read` | I/O | Read with line numbers, keyword search, fuzzy path suggestion on miss |
| 3 | `file_patch` | I/O | Exact-match string replacement; verifies uniqueness before write |
| 4 | `file_write` | I/O | Overwrite/append/prepend with `<file_content>` tag extraction |
| 5 | `web_scan` | Browser | Simplified HTML + tab list via CDP; cuts hidden/floating elements |
| 6 | `web_execute_js` | Browser | Full JS injection; save results to file for long outputs |
| 7 | `web_search` | Web | Multi-backend: DuckDuckGo → Bing; `curl_cffi` Chrome TLS impersonation |
| 8 | `web_fetch` | Web | HTTP GET with HTML stripping; entity decoding; configurable max chars |
| 9 | `update_working_checkpoint` | Memory | Short-term notepad; auto-injects each turn for context continuity |
| 10 | `ask_user` | Control | Interrupt task for human input with optional quick-select candidates |
| 11 | `start_long_term_update` | Memory | Structured L2/L3 memory distillation after task completion |

### Web Search Architecture
```
web_search(query)
  ├── Try curl_cffi (Chrome TLS fingerprint)
  │   ├── DDG Lite  → _parse_ddg_lite()
  │   ├── DDG HTML  → _parse_ddg_html()
  │   └── Bing      → _parse_bing()
  ├── Fallback: urllib (standard TLS)
  │   └── Same backends in order
  └── All fail → error with guidance
```

### Code Execution Sandbox
- **Python**: Writes to `temp/{random}.ai.py`, runs via `subprocess.Popen`, cleans up after
- **PowerShell**: Runs single-line mode with UTF-8 encoding prefix, no profile
- **Streaming**: Real-time stdout capture via background thread
- **Timeout**: Configurable (default 60s), hard-kills after timeout
- **Stop signal**: Checked during execution; kills process on user abort

---

## 5. Memory System

### Layer Architecture

```
L0 ─ memory/memory_management_sop.md
     Four core axioms:
     1. Action-Verified Only — only info confirmed by tool execution
     2. Sanctity of Verified Data — never modify verified facts
     3. No Volatile State — nothing stored that dies with the session
     4. Minimum Sufficient Pointer — L1 ≤30 lines, ≤1k tokens

L1 ─ memory/global_mem_insight.txt (25 lines)
     Compact routing index. Startup read for every session.
     Routes: Startup | Supervision | L0 | L2 | L3 | L4 | Web/UI | Ops | Project | MemTools

L2 ─ memory/global_mem.txt
     Stable environment facts + auto-mined discoveries.
     Sections:
     - ## L4-Mined Facts (auto) — incremental, append-only, cross-run dedup
     - ## Task Lessons Learned (auto) — extracted by lesson_learned.py

L3 ─ memory/*_sop.md (41 files)
     Reusable standard operating procedures.
     Categories: Ops/Goals | Audit/Monitoring | Dev | Memory | Workflow | Web/UI

L4 ─ memory/L4_raw_sessions/
     all_histories.txt — aggregated raw user messages from compressed sessions
     history_insight/ — persistent mining state
     scheduler.py pipeline: every 60s archive, every 10min mine
```

### L4 → L2 Mining Pipeline

```
temp/model_responses/model_responses_*.txt
       │  (scheduler.py every 60s)
       ▼
compress_session.py → L4_raw_sessions/{session}.zip + all_histories.txt
       │  (scheduler.py every 10min)
       ▼
salient_mining.py
  ├── _parse_sessions()         # Split by SESSION marker
  ├── _extract_facts()          # Pattern-match SOP refs, emotions, activities
  ├── _merge_activity_knowledge() # Update activity_knowledge.json
  └── _update_l2()              # Append to global_mem.txt (cross-run dedup)
       │
       ▼
history_insight/
  ├── processed_session.txt     # Last processed session (incremental tracking)
  ├── activity_knowledge.json   # Ongoing/resolved activities
  └── emotional_events.json     # Append-only emotion log
```

### Key Design Decisions
- **L1 ≤30 lines** hard cap: forces discipline, prevents index rot
- **Incremental L4 mining**: Only processes sessions after last `processed_session`
- **Cross-run dedup**: `_l2_dedup_cache` prevents duplicate L2 entries across scheduler runs
- **Append-only patterns**: L2 mined facts and emotional events are append-only; never modify existing content
- **File access tracking**: `file_access_stats.json` records every `file_read` to `memory/*` — powers stale SOP detection

---

## 6. Autonomy Modules (`reflect/`)

### Scheduler (`scheduler.py`)
- Two cron loops: L4 archiving (60s) + salient mining (600s)
- Task scheduling: reads `sche_tasks/*.json`, triggers on schedule match
- Cooldown per task: daily (20h), weekly (6d), custom (every_Nh/Nm)
- Execution window: skips tasks if machine was off past `max_delay_hours`
- Port lock: binds `127.0.0.1:45762` to prevent duplicate scheduler processes

### Goal Mode (`goal_mode.py`)
- Time-budgeted autonomous goal loop
- Each turn: probe → design → execute → check
- `GOAL_STATE` env var points to persistent state JSON
- Instability braking: detects worker spin without progress

### Goal Hive (`goal_hive_sop.md` + `assets/agent_bbs.py`)
- Multi-agent coordination via local BBS (FastAPI server)
- Master/Worker protocol: Master decomposes + aggregates; Workers execute independently
- `goal_state.json`: objective, phases, worker assignments
- BBS: on-demand HTTP board with post/poll/register/file-upload APIs
- Single-port per mission: key-based board isolation

### Proactive Monitor (`proactive_monitor.py`)
- 4-channel reflector: disk space, memory pressure, stale SOPs, dirty git
- Configurable thresholds: `--disk-threshold` %, `--mem-threshold` %, `--interval` seconds
- Alerts via task injection: agent prompts user with specific actions
- Logging: `sche_tasks/proactive_monitor.log`

---

## 7. Frontends

### TUI (`frontends/tui/`)
- **tuiapp_v2.py** — 6070-line Textual app
  - Sidebar with session history
  - Fold mode for long outputs (collapsible turn blocks)
  - Command bar: `/help`, `/status`, `/sessions`, `/new`, `/switch`, `/close`, `/rename`, `/branch`, `/rewind`, `/clear`, `/stop`, `/llm`, `/export`, `/restore`, `/btw`, `/review`, `/continue`, `/cost`, `/reload-keys`
  - `/scheduler` — manage scheduled tasks
  - Theme cycle: ga-default, nord, gruvbox, dracula, tokyo-night, textual-light
  - **Split**: `tui_base.py` (1228 lines: utilities + themes + CSS) + `tui_widgets.py` (1276 lines: ChatMessage + widgets + bars)
- **tui_v3.py** — Prompt_toolkit-based experimental scrollback-first TUI

### Streamlit (`frontends/streamlit/stapp.py`)
- "Cowork" web UI
- Collapsible turn blocks with auto-fold for LLM Running markers
- Sidebar: LLM selection, force stop, reinject tools, desktop pet launcher, autonomous toggle
- `/new`, `/continue`, `/btw`, `/export` commands via chat input
- Scroll height ghost fix + IME composition fix

### Desktop (`frontends/desktop/`)
- **qtapp.py** — Qt-based desktop GUI
- **desktop_pet_v2.pyw** — Interactive desktop companion using Tkinter
- **desktop_bridge.py** — Cross-app communication bridge

### Chat Bots (`frontends/chat/`)
- Telegram (`tgapp.py`), WeChat (`wechatapp.py`), DingTalk (`dingtalkapp.py`), QQ (`qqapp.py`), Feishu (`fsapp.py`), WeCom (`wecomapp.py`)
- Shared infrastructure: `chatapp_common.py`, `session_names.py`

---

## 8. Task Modes

### Interactive CLI
```bash
python agentmain.py
> 有什么任务？
```
Normal chat loop: user prompt → agent processing → response. `/session.xxx=yyy` for runtime config.

### Batch Task Mode (`--task --once`)
```bash
python agentmain.py --task my_task --nobg --once
```
1. Reads `temp/my_task/input.txt`
2. Runs agent loop with task content as user prompt
3. Writes `done.json` with status/exit_code/rounds
4. Archives session to L4
5. Exits

**Task supervision**: `python memory/task_watchdog.py <task_dir> --json`
- Polls `done.json` with configurable timeout/interval
- Cross-platform PID verification (wmic on Windows, /proc on Linux)
- State machine: running → completed / error / timeout
- Exit codes: 0=completed, 1=error, 2=timeout, 3=invalid

### Reflect Mode (`--reflect`)
```bash
python agentmain.py --reflect reflect/proactive_monitor.py
```
- Loads a Python module with `check()` function
- Calls `check()` at `INTERVAL` seconds
- When `check()` returns a prompt string → injects as agent task
- Supports hot-reload: re-imports module on file change

---

## 9. Plugin System (`plugins/`)

### Hook Points
- `discover_and_load()` — Scans and imports plugin modules at startup
- `trigger(event_name, locals_dict)` — Fires hooks at: `agent_before`, `turn_before`, `llm_before`, `llm_after`, `tool_before`, `tool_after`, `turn_after`, `agent_after`

### Available Hooks
- **Langfuse tracing** (`langfuse_tracing.py`) — Optional observability integration
- Extensible: add hook handlers in plugin modules

---

## 10. Safety Architecture

### Sandbox Policy
- **Only pre-approved write location**: `D:\GenericAgent\sandbox`
- **All other paths**: read-only by default, require explicit user confirmation
- **Confirmation format**: path + action + reason + verification
- **Sandbox structure**:
  - `workspace/` — drafts, experiments, generated files
  - `reports/` — read-only analysis outputs
  - `inbox/` — user-provided inputs
  - `trash_review/` — proposed deletions (never delete originals)

### Runtime Safety
- **os.kill/os.system**: 0 occurrences in codebase (confirmed by audit)
- **Hardcoded paths**: 2 legacy bash.exe paths (non-critical)
- **API keys**: never stored in memory files; stat-only access
- **Process isolation**: `code_run` uses temp files, never eval() on untrusted input
- **3-failure escalation**: stop after 3 consecutive failures, ask user

### Config Validation
- `config_check.py` runs at launch (non-blocking) — 18 checks:
  - Environment: Python version, project root
  - Imports: agent_loop, llmcore, ga, GenericAgent instantiation
  - Config: mykey.py existence, API key env, proxy
  - Tools: schema JSON, sys_prompt, web connectivity, web_search
  - Memory: L1 lines, SOP count, L4 archive size
  - System: disk space (cross-platform via shutil), git status

---

## 11. Data Flow — Complete Task Cycle

```
1. User creates temp/{task_name}/input.txt

2. agentmain.py --task {task_name} --nobg --once
   ├── resolve_task_dir() → absolute path under temp/
   ├── Spawn background agent process (subprocess.Popen)
   ├── Write PID to temp/{task}/pid
   ├── Redirect stdout/stderr to temp/{task}/stdout.log, stderr.log
   └── Exit parent

3. Agent process (--nobg --once)
   ├── Read input.txt
   ├── agent_runner_loop()
   │   ├── system_prompt + global_memory()
   │   ├── Turn 1: user message = input.txt content
   │   ├── Turn N: LLM response → tool calls → results → next prompt
   │   └── ...
   ├── _write_done_json(status=completed, rounds=N)
   ├── _archive_session_to_l4(log_path)
   │   ├── compress_session.py → L4 zip
   │   └── Append to all_histories.txt
   └── Exit

4. task_watchdog.py polls done.json
   ├── Read pid file → cross-platform cmdline check
   ├── Parse done.json status/exit_code/rounds
   └── Exit with appropriate code

5. Scheduler (background, separate process)
   ├── Every 60s: batch_process model_responses → L4 zip + all_histories
   └── Every 10min: salient_mining.py → extract facts → update L2

6. lesson_learned.py (post-task)
   ├── Read done.json + output.txt
   ├── Extract error patterns, fallback chains, tool insights
   └── Write to L2 under ## Task Lessons Learned (auto)
```

---

## 12. Evolution Methodology

### Three-Pass SOP Crystallization
1. **Pass 1**: Complete the task, record friction points
2. **Pass 2**: Repeat using friction notes, reduce steps (target: 40% fewer turns)
3. **Pass 3**: Crystallize into reusable SOP with trigger words, inputs, success criteria, failure recovery

### Phase 2 Evolution Domains (2026-06-11)
| Domain | Goals | Status |
|--------|:----:|:------:|
| 1. Autonomous Operation | 4 | ✅ 4/4 |
| 2. Web & Research | 4 | ✅ 4/4 |
| 3. Memory Evolution | 4 | ✅ 4/4 |
| 4. Code Quality | 4 | ✅ 4/4 |
| 5. Human-Agent Collaboration | 4 | ✅ 4/4 |

### Key Metrics (Current)
- **SOP count**: 41 (from 0 at cold start)
- **Tools**: 11 (from 9 at cold start)
- **Automated cron jobs**: 3
- **Config checks**: 18
- **L4 archived sessions**: 58+
- **L2 mined facts**: 29+
- **Task lessons extracted**: 38
- **Git commits**: 24 (across 3 days)
- **Test coverage**: 10 supervised rounds, 3 P2 rounds, all passed

---

## 13. Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.1.0 | 2026-06-11 | Cold start complete. L0-L4 memory, 11 tools, 41 SOPs, 7 frontends, 3 cron jobs, multi-provider LLM, web search, proactive monitoring. |
