# Personal Bootstrap Profile

## Purpose

This file is the stable startup profile for this GenericAgent instance. Read it before broad autonomous work, memory updates, local project maintenance, or skill crystallization.

## Environment

- **Workspace root:** `/opt/GenericAgent`
- **Host OS:** Linux (Alibaba Cloud ECS, 2-core, 1.6GB RAM)
- **Python:** 3.12.3 at `/opt/GenericAgent/venv/bin/python`
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
- **Runtime services:** `genericagent.service`, `genericagent-napcat.service`, and `genericagent-qq.service` under systemd.
- **Previous WSL/Windows environment:** historical only; retain it in L4 raw archives, never treat its paths or tool versions as current cloud facts.

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
- Prefer read-only observation of real projects and write inside `/opt/GenericAgent/sandbox` unless the user explicitly approves another path or a narrower controlled capability applies.

## Safety Boundaries

- Do not perform payment, ordering, destructive file deletion, credential changes, or outbound messaging without explicit confirmation.
- Do not store API keys, passwords, cookies, or private tokens in memory files.
- Prefer read-only exploration before automation that clicks, sends, deletes, buys, or modifies external accounts.
- General writes are pre-approved only inside `/opt/GenericAgent/sandbox`.
- After explicit user approval, the controlled memory path may patch existing top-level `.md`, `global_mem.txt`, and `global_mem_insight.txt`, or create a new top-level `.md`.
- Memory overwrite, append/prepend, Python/JSON changes, nested paths, symlink escapes, L4 raw-session edits, and `code_run` memory writes remain denied.
- Treat all other non-sandbox paths as read-only by default.
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
| v3 | 2026-08-06 | Linux 化：环境基线确认为 Ubuntu 24.04 root，bash 环境 |
