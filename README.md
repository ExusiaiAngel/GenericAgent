# GenericAgent

A minimal self-evolving autonomous agent framework with L0-L4 layered memory, multi-backend web search, and 41 crystallized SOPs (standard operating procedures).

Built in Python 3.13, runs on Windows 11 / WSL / Linux. MIT licensed.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  agentmain.py           ┌─── agent_loop.py (130 lines)  │
│  CLI / Task / Reflect   │   Turn-based agent runner     │
│                         │   Tool dispatch by convention │
│                         └───────────────────────────────│
│  llmcore.py (1063 lines)                               │
│  Multi-provider LLM session: DeepSeek, Claude, OpenAI   │
│                                                         │
│  ga.py — 11 tools via function-call schema              │
│  ┌─────────────┬──────────────┬──────────────────────┐  │
│  │ code_run    │ file_read    │ file_patch           │  │
│  │ file_write  │ web_scan     │ web_execute_js       │  │
│  │ web_search  │ web_fetch    │ update_working_ckpt  │  │
│  │ ask_user    │ start_long_term_update              │  │
│  └─────────────┴──────────────┴──────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## Key Features

### 11-Tool Agent Core
- **Code execution** — Python & PowerShell in isolated temp files
- **File I/O** — Read with keyword search, patch with exact-match replacement, write with append/overwrite
- **Web search** — Multi-backend (DuckDuckGo → Bing) with Chrome TLS impersonation via `curl_cffi`
- **Web fetch** — HTML stripping, entity decoding, configurable max chars
- **Browser control** — `web_scan` (simplified HTML + tab management) and `web_execute_js` (full JS injection) via CDP bridge
- **Working memory** — Short-term checkpoint that auto-injects each turn to prevent context loss
- **Long-term update** — Structured memory distillation hook

### L0-L4 Memory System
| Layer | Location | Purpose |
|:------|----------|--------|
| **L0** | `memory/memory_management_sop.md` | Four core axioms: Action-Verified Only, Sanctity of Verified Data, No Volatile State, Minimum Sufficient Pointer |
| **L1** | `memory/global_mem_insight.txt` | Compact startup index (≤25 lines) routing to all modules |
| **L2** | `memory/global_mem.txt` | Stable environment facts + L4-mined auto-facts |
| **L3** | `memory/*_sop.md` | 41 crystallized SOPs across 6 categories |
| **L4** | `memory/L4_raw_sessions/` | Raw session archives with automated mining pipeline |

### Three Operating Modes
```bash
# Interactive (TUI / CLI)
python agentmain.py

# Batch task mode (input.txt → agent → done.json)
python agentmain.py --task <dir> --nobg --once

# Reflect mode (periodic check loop)
python agentmain.py --reflect reflect/proactive_monitor.py

# CLI shortcut
ga.cmd          # Windows
python -m ga_cli # cross-platform
```

### Automated Infrastructure
- **L4 Archiving** — scheduler.py every 60s: compress_session.py → `all_histories.txt`
- **Salient Mining** — scheduler.py every 10min: `salient_mining.py` extracts SOP refs, emotional events, activity patterns → `global_mem.txt`
- **Scheduled Tasks** — 3 cron jobs in `sche_tasks/`:
  - `daily_brief` at 08:57 daily
  - `health_check` at 09:03 daily
  - `backup_verify` at 08:02 weekly (Sunday)
- **Config Validator** — `config_check.py` runs at launch (non-blocking): 18 checks across env/imports/config/tools/memory/system
- **Proactive Monitor** — `reflect/proactive_monitor.py`: disk/memory/stale SOPs/dirty git alerts
- **Lesson Learning** — `memory/lesson_learned.py`: extracts failure patterns and tool fallback chains from completed tasks → L2
- **Stale SOP Detector** — `memory/stale_sop_check.py`: 30-day threshold via `file_access_stats.json`

### Frontend Options
| Frontend | Path | Tech |
|----------|------|------|
| Streamlit Cowork | `frontends/streamlit/stapp.py` | Streamlit web UI |
| Textual TUI | `frontends/tui/tuiapp_v2.py` | Rich terminal UI (6070 lines) |
| Qt Desktop | `frontends/desktop/qtapp.py` | PySide6 |
| Desktop Pet | `frontends/desktop/desktop_pet_v2.pyw` | Tkinter |
| Hub | `hub.pyw` | Multi-agent launcher |
| IM Bots | `frontends/chat/` | Telegram, WeChat, DingTalk, QQ, Feishu |

---

## Quick Start

### Prerequisites
- Python 3.10–3.13
- Git
- A DeepSeek API key (or other LLM provider — see `mykey_template.py`)

### Install
```powershell
# Clone
git clone https://github.com/lsdefine/GenericAgent.git
cd GenericAgent

# Install core dependencies
pip install -e .

# Optional: UI
pip install -e ".[ui]"

# Configure API key (edit mykey.py or set env var)
# See mykey_template.py for multi-provider examples
```

### Launch
```powershell
python agentmain.py          # Interactive CLI
python agentmain.py --task my_task --nobg --once  # Batch mode
```

### Verify
```powershell
python config_check.py       # Run 18-point self-diagnostic
```

---

## Memory SOP Categories

| Category | Count | Example SOPs |
|----------|:-----:|--------------|
| **Operations/Goals** | 8 | `goal_hive_sop`, `scheduled_task_sop`, `autonomous_operation_sop` |
| **Audit/Monitoring** | 6 | `env_audit_sop`, `system_monitor_sop`, `network_monitor_sop`, `health_check_sop` |
| **Development** | 5 | `git_hygiene_sop`, `github_contribution_sop`, `checklist_sop` |
| **Memory Management** | 7 | `memory_management_sop`, `memory_index_hygiene_sop`, `stale_sop_check.py` |
| **Workflow/Supervision** | 6 | `plan_sop`, `review_sop`, `verify_sop`, `deep_research_sop` |
| **Web/UI** | 6 | `tmwebdriver_sop`, `web_research_sop`, `ljqCtrl_sop` |
| **Setup/Config** | 3 | `new_machine_setup_sop`, `config_check.py`, `launch_readiness_check_sop` |

---

## Project Map

```
GenericAgent/
├── agentmain.py          # Main entrypoint (CLI + task + reflect modes)
├── agent_loop.py         # Agent turn runner (130 lines)
├── llmcore.py            # LLM session management (1063 lines)
├── ga.py                 # Tool implementations + handler (700+ lines)
├── config_check.py       # 18-point self-diagnostic
├── pyproject.toml        # Package metadata
├── assets/               # Tool schema, system prompts, CDP bridge
├── memory/               # L0-L4 memory + 41 SOPs
│   ├── global_mem_insight.txt    # L1 index
│   ├── global_mem.txt            # L2 facts
│   ├── *_sop.md                  # L3 SOPs
│   └── L4_raw_sessions/          # L4 archives + mining
├── reflect/              # Autonomy: scheduler, goal mode, proactive monitor
├── frontends/            # 7 UI frontends (TUI, Streamlit, Qt, chat bots...)
│   ├── tui/              #   Textual TUI (split: tui_base.py + tui_widgets.py)
│   ├── streamlit/        #   Streamlit web UI
│   ├── desktop/          #   Qt app + desktop pet
│   ├── chat/             #   IM bots (Telegram, WeChat, DingTalk, QQ, Feishu)
│   ├── conductor/        #   Sub-agent orchestrator
│   ├── cmd/              #   CLI commands (/continue, /export, /review, /btw)
│   └── shared/           #   Chat app common, slash commands, session names
├── plugins/              # Plugin hooks + Langfuse tracing
├── sche_tasks/           # Scheduled task configs (JSON)
├── sandbox/              # Approved write boundary
│   ├── workspace/        #   Drafts and temp work
│   ├── reports/          #   Generated reports
│   ├── inbox/            #   User-provided inputs
│   └── trash_review/     #   Proposed deletions
├── docs/                 # Documentation + superpower plans
└── temp/                 # Task directories (--task mode)
```

---

## Key Design Decisions

1. **Convention over configuration** — Tool dispatch uses `do_{tool_name}` naming; no registry needed
2. **Generator-based tool results** — Every tool is a Python generator that yields progress messages and returns `StepOutcome`
3. **Sandbox-only writes** — All writes outside `D:\GenericAgent\sandbox` require explicit user approval
4. **Three-pass SOP methodology** — Pass 1: complete + record friction → Pass 2: repeat optimized → Pass 3: crystallize
5. **Multi-backend fallback** — Web search tries DuckDuckGo → Bing with `curl_cffi` Chrome impersonation; web_fetch strips HTML
6. **Incremental L4 mining** — Only processes new sessions; dedup via cross-run cache; never rewrites existing L2 content

---

## Documentation

- `CLAUDE.md` — Project bootstrap for Claude Code sessions
- `docs/GETTING_STARTED.md` — New user setup guide
- `docs/ARCHITECTURE.md` — Full technical architecture reference
- `memory/project_map.md` — Detailed module inventory
- `memory/next_phase_goals.md` — Evolution roadmap (Phase 2: 5 domains, 16 goals)
- `memory/new_machine_setup_sop.md` — 5-step fresh-install SOP

## License

MIT — see `LICENSE`.
