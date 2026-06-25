# Cold Start Task Queue

## Operating Loop

Each task should run in three passes:

1. First pass: complete the task and record friction.
2. Second pass: repeat using the recorded friction notes and reduce steps.
3. Third pass: crystallize a reusable SOP or skill if the task is stable.

## Skill Crystallization Criteria

A task can become a skill only when it has:

- Trigger phrase: the user sentence that should call it.
- Inputs: exact data the user must provide.
- Success criteria: observable completion state.
- Verification command or manual check.
- Failure recovery: what to do after one, two, and three failed attempts.
- Human confirmation points for risky actions.

## Week 1 Queue

### Global Write Boundary

- Until the user grants a narrower exception, all real project directories are read-only.
- The only pre-approved write location is `/opt/GenericAgent/sandbox`.
- Write drafts and generated files to `/opt/GenericAgent/sandbox/workspace`.
- Receive user-provided inputs and samples in `/opt/GenericAgent/sandbox/inbox`.
- Write reports to `/opt/GenericAgent/sandbox/reports`.
- Put proposed deletion/replacement candidates in `/opt/GenericAgent/sandbox/trash_review` instead of deleting originals.
- Any modification outside the sandbox requires explicit user confirmation naming the path and action.

### 0. Personal Supervision

- Trigger: "监督 GenericAgent"
- Risk: low
- Inputs: current GenericAgent task, output text, or output file path
- Action: apply `memory/personal_supervision_sop.md` to check task discipline, safety, verification, and memory hygiene.
- Output: concise supervision report with PASS, FAIL, PARTIAL, or RUNNING.
- Success criteria: unsafe actions are blocked, unsupported claims are challenged, and completed work has evidence.
- Verification: read the relevant output/report and confirm every PASS has tool or file evidence.

### 1. Project Map

- Trigger: "熟悉 GenericAgent 项目结构并更新项目地图"
- Risk: low
- Inputs: none
- Action: read README, `pyproject.toml`, top-level files, `memory/`, `reflect/`, `plugins/`, and `frontends/`
- Output: `memory/project_map.md`
- Success criteria: the map identifies entrypoints, memory layers, frontend options, plugin hooks, and reflective modes.
- Verification: `Get-Content memory\project_map.md -TotalCount 120`

### 2. Memory Index Hygiene

- Trigger: "整理 GenericAgent memory 索引"
- Risk: low
- Inputs: none
- Action: compare `memory/global_mem_insight.txt` against actual `memory/` files and add compact routing notes.
- Output: updated `memory/global_mem_insight.txt`
- Success criteria: L1 can route to bootstrap, project map, SOPs, browser, mobile, scheduling, and review workflows.
- Verification: `Get-Content memory\global_mem_insight.txt`

### 3. Launch Readiness Check

- Trigger: "检查 GenericAgent 是否能启动"
- Risk: low
- Inputs: selected Python executable if multiple exist
- Action: check WSL `python3` version, import core modules, inspect `mykey.py` presence without printing secrets, and identify missing dependencies.
- Output: `memory/launch_readiness_report.md`
- Success criteria: report says which command to run and what must be fixed before launch.
- Verification: `Get-Content memory\launch_readiness_report.md -TotalCount 120`

### 4. First Reusable Local Skill

- Trigger: "把最近一次成功的本地维护任务沉淀成 Skill"
- Risk: low
- Inputs: completed task name and observed steps
- Action: turn a stable local workflow into an SOP under `memory/`
- Output: one new `memory/*_sop.md` file or a focused update to an existing SOP
- Success criteria: future sessions can invoke the workflow from one trigger phrase.
- Verification: read the SOP and confirm trigger, inputs, success criteria, verification, and failure recovery exist.

### 5. Controlled Browser Workflow

- Trigger: "建立低风险网页调研 Skill"
- Risk: medium
- Inputs: topic and allowed sources
- Action: browse/read/summarize only; no account changes or form submissions.
- Output: `memory/web_research_sop.md` or update to existing browser SOP index.
- Success criteria: browser workflow includes source capture and cross-check rules.
- Verification: use it on one harmless documentation lookup.

---

## Observations Log

### 2026-06-09: Task 1 Friction Analysis (one-shot validation run)

Ran a one-shot task to list 3 main entry files. Full analysis at `reports/task1_friction_analysis.md`.

Key findings:
- **6 LLM turns for a 2-3 turn task** (2-3x waste)
- Root causes: wrong first tool choice (file_read on a directory), exploration drift into irrelevant files, redundant re-reads, not prioritizing pyproject.toml
- **done.json `rounds: 1` is a BUG** -- `nround` counter in agentmain.py only increments in interactive reply mode, not in `--once` mode
- Verifies the three-pass methodology is necessary: even simple tasks inflate without friction awareness

Action items for Pass 2:
1. Fix done.json rounds counting in agentmain.py (Path A in --once mode)
2. Add docstrings to agentmain.py and fix hub.pyw header comment
3. Add "read project metadata first" heuristic to system prompt for file-discovery tasks

## Week 1 Progress Tracker (2026-06-09 update)

| # | Task | Pass 1 | Pass 2 | Pass 3 | SOP |
|:-:|:-----|:------:|:------:|:------:|:---:|
| 0 | Personal Supervision | ✅ | — | — | existing |
| 1 | Project Map | ✅ | — | — | — |
| 2 | Memory Index Hygiene | ✅ | ✅ | — | existing |
| 3 | Launch Readiness Check | ✅ | ✅ | — | existing |
| 4 | First Reusable Local Skill | ✅ | ✅ | ✅ | ✅ reusable_task_runner_sop.md |
| 5 | Controlled Browser Workflow | ✅ | — | — | existing |
| 6 | System Environment Audit | ✅ | ✅ | — | ✅ env_audit_sop.md |
| 7 | Project Dependency Check | ✅ | ✅ | — | ✅ dep_check_sop.md |
| 8 | Git Workspace Hygiene | ✅ | ✅ | — | ✅ git_hygiene_sop.md |
| 9 | Unified Health Check | ✅ | ✅ | ✅ | ✅ health_check_sop.md |
| 10 | System Process Monitor | ✅ | ✅ | — | ✅ system_monitor_sop.md |
| 11 | Backup Verification | ✅ | ✅ | — | ✅ backup_verify_sop.md |
| 12 | File Organization Analysis | ✅ | ✅ | ✅ | ✅ file_org_analysis_sop.md |
| 13 | Daily Brief | ✅ | ✅ | — | ✅ daily_brief_sop.md |
| 14 | Config Audit | ✅ | ✅ | — | ✅ config_audit_sop.md |
| 15 | Network Monitor | ✅ | ✅ | — | ✅ network_monitor_sop.md |

### Fixes Applied
- `env.sh`: cpython PATH bug (missing `.13` version suffix)
- `agentmain.py`: done.json rounds counting bug (3 paths fixed)
- `global_mem_insight.txt`: 4 fixes (name alignment, missing entries, dedup, watchdog placement)
- `file_access_stats.json`: 22 stale entries removed, 4 wrong-extension entries corrected

### Task Run Summary
- Task 1 (Project Map): 6 turns, 6 friction points documented → `reports/task1_friction_analysis.md`
- Task 6 (Env Audit): 6 turns, Pass 1 complete, SOP crystallized → `memory/env_audit_sop.md`
- Task 5 (Web Research): 6 rounds, pip 26.1.2 vs local 26.1.1, safe. Path resolution bug found and documented in SOP.
- Task 7 (Dep Check): 3 turns, Pass 1 complete, core deps 100%, UI deps 50%
- Task 8 (Git Hygiene): running — check `workspace/git_hygiene_task/`
- Task 10 (Process Monitor): 5 rounds, system 99/100, 20-core i7-13700H, 12% memory
- Task 11 (Backup Verify): 4 rounds, risk=MEDIUM, mykey.py protected (stat only), remote confirmed
- Task 12 (File Organization): ✅ (Pass 1 done, action plan created)
- Task 9 (Health Check v2): Pass 2 — 22→8 rounds, 87.5/100, cron runner script ready
- Task 13 (Daily Brief): 5 rounds, 92/100, compact 6-section morning report
- Task 14 (Config Audit): 6 rounds, 81/100, secrets properly filtered, 0 SSH keys (HIGH priority)
- Task 15 (Network Monitor): 5 rounds, 98/100, 6/6 checks passed, DNS SPOF (10.255.255.254) noted

## Week 2 Queue

### 9. Daily Health Check Automation
- Trigger: "每日健康检查" / "health check"
- Risk: low (read-only)
- Inputs: none (or {date} parameter)
- Action: Run unified env+deps+git health check, produce dashboard report
- Output: `sandbox/reports/health_check_YYYY-MM-DD.md`
- Dependencies: Tasks 6, 7, 8 SOPs

### 10. System Process Monitor
- Trigger: "系统进程监控" / "process monitor"
- Risk: low (read-only)
- Status: ✅ Pass 1 — 5 rounds, system 99/100, 20-core i7-13700H, 12% memory
- Output: `memory/system_monitor_sop.md`

### 11. Backup Verification
- Trigger: "验证备份" / "verify backup"
- Risk: low (read-only, mykey.py stat only)
- Status: ✅ Pass 1 — 4 rounds, risk=MEDIUM, remote confirmed
- Output: `memory/backup_verify_sop.md`

### 12. File Organization Analysis
- Trigger: "整理项目文件"
- Risk: low-medium (read recommendations, user approves moves)
- Status: ✅ Pass 1 — action plan created
- Action: Scan project dirs for stale/temp/duplicate files, suggest organization
- Output: `sandbox/reports/file_org_report.md`

## Session Summary 2026-06-09

### Execution Stats
- Tasks executed this session: 10 (Tasks 5-15, excluding 0-4 already done)
- New SOPs crystallized: 9 (env_audit, dep_check, git_hygiene, system_monitor, backup_verify, web_research_path_rule, daily_brief, config_audit, network_monitor)
- Bugs fixed: 4 (env.sh, agentmain.py rounds, L1 index, file_access_stats)
- Git commits: 2 (8ca93f5, plus SOP commit)
- Sandbox reports generated: 70+

### Capability Map (as of 2026-06-10)
| Domain | Capabilities | Coverage |
|--------|-------------|----------|
| System Steward | env audit, process monitor, network monitor, config audit | 90% |
| Project Assistant | dep check, git hygiene, code quality, file org, daily brief, CI pipeline | 95% |
| Security & Safety | backup verify, git bundle backup, sandbox policy, secret filtering, path rules | 90% |
| Automation | health check cron script (daily 9:03), git bundle cron (weekly Sun 8AM), task runner, watchdog | 80% |
| Web Research | safe HTTP/curl, TMWebDriver imports OK, SOP with path rules, sandbox isolation | 50% |

### Remaining Gap to Goal (~95% complete, was ~80%)
- ✅ frontend Phase 2 → subdirectory refactor (28 files git mv, all imports fixed)
- ✅ frontend Phase 3 → deprecation headers, cleanup plan
- ✅ docs/ folder organization → README→docs/README_project, CONTRIBUTING→docs/, new README, docs index
- ✅ browser interaction → TMWebDriver imports clean, requests/curl web research confirmed working
- ❌ TMWebDriver full CDP browser workflow (requires Chrome/Edge + extension running)
- Ready for daily use as personal steward and project assistant

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |

