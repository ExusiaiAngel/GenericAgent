# GenericAgent

## Bootstrap (Read these first)
- **Personal Bootstrap:** `memory/personal_bootstrap_profile.md` — environment, API config, safety boundaries, user preferences
- **Memory Index (L1):** `memory/global_mem_insight.txt` — routes to all L2/L3 files (SOPs, tools, web, mobile). Keep under 30 lines
- **Memory SOP (L0):** `memory/memory_management_sop.md` — four core axioms (Action-Verified Only, Sanctity of Verified Data, No Volatile State, Minimum Sufficient Pointer) and L1/L2/L3/L4 hierarchy
- **Cold Start Queue:** `memory/cold_start_task_queue.md` — evolution tasks, skill crystallization criteria, three-pass methodology
- **Project Map:** `memory/project_map.md` — architecture overview, entrypoints, modules, frontends

## Safety Rules (Non-Negotiable)
1. The only pre-approved write location is `D:\GenericAgent\sandbox`. All other project directories are read-only by default
2. Never modify/delete/overwrite real project files outside sandbox without explicit user approval. Required format: path + action + reason + verification
3. No payments, destructive deletions, credential changes, or outbound messaging without confirmation
4. Never store API keys, passwords, or tokens in memory files
5. Stop after 3 consecutive failures and ask for user intervention
6. Before broad autonomous work, read personal_bootstrap_profile.md + cold_start_task_queue.md + global_mem_insight.txt

## Common Commands
- `python agentmain.py` — primary entrypoint
- `python -m ga_cli` or `ga.cmd` — CLI shortcut
- `python hub.pyw` — hub GUI
- `python agentmain.py --task <name> --nobg --once` — run task once (dir under `temp/`)
- `python memory/task_watchdog.py <task_dir> --json` — supervise task

## L4 Memory Pipeline (auto)
- L4 archive: scheduler.py every 60s → compress_session.py → memory/L4_raw_sessions/
- Salient mining: scheduler.py every 10min → salient_mining.py → history_insight/ + global_mem.txt
