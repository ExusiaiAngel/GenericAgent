# Personal Bootstrap Profile

## Purpose

This file is the stable startup profile for this GenericAgent instance. Read it before broad autonomous work, memory updates, local project maintenance, or skill crystallization.

## Environment

- **Workspace root:** `/opt/GenericAgent`
- **Host OS:** Linux (Alibaba Cloud ECS, 2-core, 1.6GB RAM)
- **Python:** 3.13.13 at `/usr/bin/python3`
- **Shell:** bash
- **Project:** GenericAgent, a minimal self-evolving autonomous agent framework.
- **Primary entrypoint:** `python agentmain.py`
- **CLI shortcut:** `python -m ga_cli` or `ga.sh`
- **CLI package entrypoint:** `ga = ga_cli.cli:main`
- **Core evolution directories:** `memory/`, `reflect/`, `plugins/`, `frontends/`, `docs/`
- **Approved write sandbox:** `/opt/GenericAgent/sandbox`
- **Sandbox subdirectories:**
  - `sandbox/inbox`: user-provided inputs and copied samples.
  - `sandbox/workspace`: drafts, experiments, generated files, and temporary project work.
  - `sandbox/reports`: read-only analysis outputs and supervision reports.
  - `sandbox/trash_review`: files proposed for deletion or replacement; never delete originals without confirmation.
- **TMWebDriver CDP Bridge:** Installed in Chromium (Profile: Exusiai), path: `assets/tmwd_cdp_bridge/`
- **Previous environment:** Previous workspace at `\\wsl.localhost\Ubuntu-24.04\home\exusiai\GenericAgent`, no longer active. `env.sh` retains WSL paths for Git Bash fallback.

## API Config

- **Provider:** DeepSeek (via `mykey.py`)
- **Endpoint:** `https://api.deepseek.com/v1`
- **Model:** `deepseek-v4-flash`
- **Auth:** `DEEPSEEK_API_KEY` env var (never hardcode, never log)
- **Proxy:** `GENERICAGENT_PROXY` env var (HTTP proxy for API calls)
- **Key file:** `mykey.py` reads these env vars and exposes them to `llmcore`

## User Preference

- The user wants GenericAgent to evolve from cold start inside its own project files.
- Favor concrete project-local memory and SOP growth over broad abstract architecture work.
- Prefer Chinese for planning and operational summaries unless code or upstream docs are clearer in English.
- Keep early evolution low-risk and observable.
- During the cold-start supervision phase, prefer read-only observation of real projects and write only inside `/opt/GenericAgent/sandbox` unless the user explicitly approves another path.

## Safety Boundaries

- Do not perform payment, ordering, destructive file deletion, credential changes, or outbound messaging without explicit confirmation.
- Do not store API keys, passwords, cookies, or private tokens in memory files.
- Prefer read-only exploration before automation that clicks, sends, deletes, buys, or modifies external accounts.
- Do not modify files outside `/opt/GenericAgent/sandbox` unless the user explicitly approves the exact path and action.
- The only pre-approved write location is `/opt/GenericAgent/sandbox`.
- Treat all non-sandbox paths as read-only by default.
- For repeated failures, stop after three attempts and ask the user for intervention.

## First Evolution Domains

1. Project self-understanding and maintenance.
2. Memory hygiene and skill indexing.
3. Local file organization and documentation.
4. Browser or web research workflows only after the local loop is stable.
5. External app automation only after a clear SOP and human confirmation points exist.

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |

