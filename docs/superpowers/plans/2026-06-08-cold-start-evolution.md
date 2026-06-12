# Cold Start Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn this fresh GenericAgent instance into a personal, self-improving assistant by seeding stable memory, running low-risk repeated tasks, and crystallizing successful runs into reusable skills.

**Architecture:** Keep the seed small and project-native: L2 stores stable user/project facts, L1 routes to the new bootstrap files, and L3 grows only from verified task repetitions. The first evolution loop avoids high-risk external side effects and prioritizes read-only exploration, local organization, and explicit human-confirmed actions.

**Tech Stack:** Python 3.11/3.12, WSL `python3 agentmain.py`, `memory/` layered memory, existing SOP files, local Markdown plans.

---

## File Structure

- Create: `memory/personal_bootstrap_profile.md`
  - Stores stable facts for this user's GenericAgent instance: environment, preferences, safety boundaries, and first domains.
- Create: `memory/cold_start_task_queue.md`
  - Stores the ordered first-week task queue, success criteria, and skill crystallization rules.
- Modify: `memory/global_mem.txt`
  - Adds a compact L2 pointer to the bootstrap profile and current evolution objective.
- Modify: `memory/global_mem_insight.txt`
  - Adds L1 routing entries so future sessions know which files to read for cold-start evolution.
- Create: `docs/superpowers/plans/2026-06-08-cold-start-evolution.md`
  - This execution plan.

---

### Task 1: Seed Personal Bootstrap Profile

**Files:**
- Create: `memory/personal_bootstrap_profile.md`
- Modify: `memory/global_mem.txt`
- Modify: `memory/global_mem_insight.txt`

- [x] **Step 1: Create the bootstrap profile**

Write `memory/personal_bootstrap_profile.md` with:

```markdown
# Personal Bootstrap Profile

## Purpose

This file is the stable startup profile for this GenericAgent instance. Read it before broad autonomous work, memory updates, local project maintenance, or skill crystallization.

## Environment

- Workspace root: `\\wsl.localhost\Ubuntu-24.04\home\exusiai\GenericAgent`
- Current OS surface: Windows PowerShell against a WSL Ubuntu workspace path.
- Project: GenericAgent, a minimal self-evolving autonomous agent framework.
- Recommended Python versions from project docs: Python 3.11 or 3.12; avoid Python 3.14.
- Primary local entrypoint in WSL/Linux: `python3 agentmain.py`
- CLI package entrypoint: `ga = ga_cli.cli:main`
- Core evolution directories: `memory/`, `reflect/`, `plugins/`, `frontends/`, `docs/`

## User Preference

- The user wants GenericAgent to evolve from cold start inside its own project files.
- Favor concrete project-local memory and SOP growth over broad abstract architecture work.
- Prefer Chinese for planning and operational summaries unless code or upstream docs are clearer in English.
- Keep early evolution low-risk and observable.

## Safety Boundaries

- Do not perform payment, ordering, destructive file deletion, credential changes, or outbound messaging without explicit confirmation.
- Do not store API keys, passwords, cookies, or private tokens in memory files.
- Prefer read-only exploration before automation that clicks, sends, deletes, buys, or modifies external accounts.
- For repeated failures, stop after three attempts and ask the user for intervention.

## First Evolution Domains

1. Project self-understanding and maintenance.
2. Memory hygiene and skill indexing.
3. Local file organization and documentation.
4. Browser or web research workflows only after the local loop is stable.
5. External app automation only after a clear SOP and human confirmation points exist.
```

- [x] **Step 2: Add compact L2 memory**

Append a small section to `memory/global_mem.txt`:

```markdown

## Cold-start evolution seed - 2026-06-08

This GenericAgent instance is being evolved as a personal assistant inside its own project workspace. For startup context, read `memory/personal_bootstrap_profile.md`; for the first-week execution queue, read `memory/cold_start_task_queue.md`. Keep early work low-risk, project-local, and skill-crystallization oriented.
```

- [x] **Step 3: Add L1 routing**

Add to `memory/global_mem_insight.txt`:

```text
Cold-start evolution: read personal_bootstrap_profile.md + cold_start_task_queue.md before broad autonomous work.
Bootstrap profile: environment/preferences/safety boundaries for this user.
Task queue: first-week low-risk tasks and skill crystallization rules.
```

- [x] **Step 4: Verify profile exists**

Run:

```powershell
Get-Content memory\personal_bootstrap_profile.md -TotalCount 80
```

Expected: the file shows Environment, User Preference, Safety Boundaries, and First Evolution Domains sections.

---

### Task 2: Seed First-Week Task Queue

**Files:**
- Create: `memory/cold_start_task_queue.md`

- [x] **Step 1: Create the queue**

Write `memory/cold_start_task_queue.md` with:

```markdown
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
- Action: check Python version, import core modules, inspect `mykey.py` presence without printing secrets, and identify missing dependencies.
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
```

- [x] **Step 2: Verify queue exists**

Run:

```powershell
Get-Content memory\cold_start_task_queue.md -TotalCount 160
```

Expected: the output includes Operating Loop, Skill Crystallization Criteria, and Week 1 Queue.

---

### Task 3: Run Launch Readiness Probe

**Files:**
- Create: `memory/launch_readiness_report.md`

- [x] **Step 1: Check Python version**

Run in WSL/Linux:

```powershell
python3 --version
```

Expected: Python reports 3.10, 3.11, 3.12, or 3.13. Prefer 3.11 or 3.12 for UI dependencies.

- [x] **Step 2: Check package metadata**

Run in WSL/Linux:

```powershell
python3 -m pip show genericagent
```

Expected: either installed package metadata appears, or pip reports the package is not installed. If not installed, use editable install later.

- [x] **Step 3: Probe imports without starting the agent loop**

Run:

```powershell
python3 -c "import agent_loop, llmcore; print('core imports ok')"
```

Expected: `core imports ok`, or a missing dependency error that can be copied into the readiness report.

- [x] **Step 4: Write report**

Create `memory/launch_readiness_report.md`:

```markdown
# Launch Readiness Report

## Result

- Python version: record exact `python3 --version` output.
- Editable install: record `python3 -m pip show genericagent` result.
- Core imports: record success or the exact missing dependency.
- API config: confirm whether `mykey.py` exists, but do not print secrets.

## Recommended Launch Command

```powershell
python3 agentmain.py
```

## Fixes Before Launch

- If imports fail because dependencies are missing, install only the required minimal dependency or `python3 -m pip install -e .`.
- If WSL reports `No module named pip`, install `python3-pip` first.
- If UI is needed, install `python3 -m pip install -e ".[ui]"`.
- If API config is missing, copy `mykey_template.py` to `mykey.py` and fill one native provider config.
```

- [x] **Step 5: Verify report**

Run:

```powershell
Get-Content memory\launch_readiness_report.md
```

Expected: report includes exact outputs and a recommended launch command.

---

### Task 4: Create Project Map

**Files:**
- Create: `memory/project_map.md`

- [x] **Step 1: Read top-level project files**

Run:

```powershell
Get-ChildItem -Force
```

Expected: output includes `agentmain.py`, `agent_loop.py`, `llmcore.py`, `ga.py`, `memory/`, `plugins/`, `reflect/`, `frontends/`, and `docs/`.

- [x] **Step 2: Write project map**

Create `memory/project_map.md`:

```markdown
# GenericAgent Project Map

## Primary Entrypoints

- `agentmain.py`: primary command-line agent startup.
- `agent_loop.py`: compact autonomous execution loop.
- `ga.py`: large convenience/core script surface.
- `ga_cli/cli.py`: installed `ga` CLI command.
- `launch.pyw` and `hub.pyw`: desktop launch surfaces.

## Core Runtime

- `llmcore.py`: LLM session and model integration core.
- `simphtml.py`: HTML/browser utility surface.
- `TMWebDriver.py`: browser automation support.
- `plugins/hooks.py`: plugin hook extension point.
- `plugins/langfuse_tracing.py`: optional tracing integration.

## Memory And Evolution

- `memory/global_mem_insight.txt`: compact L1 routing index.
- `memory/global_mem.txt`: stable L2 facts.
- `memory/*_sop.md`: L3 reusable workflows.
- `memory/L4_raw_sessions/`: archived raw sessions for long-horizon recall.
- `reflect/goal_mode.py`: time-budgeted self-driven goal loop.
- `reflect/scheduler.py`: scheduled execution support.

## Frontends

- `frontends/tui_v3.py`: current TUI.
- `frontends/tui/tuiapp_v2.py`: older Textual TUI.
- `frontends/qtapp.py`: Qt UI.
- `frontends/wechatapp.py`, `tgapp.py`, `dingtalkapp.py`, `wecomapp.py`, `qqapp.py`: bot/chat integrations.
- `frontends/conductor.py`: sub-agent orchestration frontend.

## Cold-Start Rule

Before broad autonomous work, read:

1. `memory/personal_bootstrap_profile.md`
2. `memory/cold_start_task_queue.md`
3. `memory/global_mem_insight.txt`
```

- [x] **Step 3: Verify project map**

Run:

```powershell
Get-Content memory\project_map.md -TotalCount 120
```

Expected: map includes entrypoints, runtime, memory, reflect, plugins, and frontend sections.

---

## Self-Review

- Spec coverage: the user's request to plan and start is covered by this saved plan plus seeded memory/profile/queue files.
- Placeholder scan: no `TBD`, `TODO`, or "implement later" placeholders are present.
- Type consistency: all referenced paths are Markdown files or existing project files.
- Risk check: early tasks are local/read-only except explicit memory documentation updates.

## Execution Mode

This plan has already begun inline because the user requested planning and startup together. Continue with `executing-plans` or direct task execution from Task 3 onward.
