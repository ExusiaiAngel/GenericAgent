# Sandbox Policy

## Current Mode

GenericAgent is a supervised cloud service. General project writes remain
read-only by default; narrowly scoped memory-content writes use a separate
controlled capability.

Real project directories are read-only by default. The general pre-approved write location is:

```text
/opt/GenericAgent/sandbox
```

## Allowed Without Asking

- Read files under `/opt/GenericAgent`.
- Create drafts under `/opt/GenericAgent/sandbox/workspace`.
- Create reports under `/opt/GenericAgent/sandbox/reports`.
- Copy user-provided samples into `/opt/GenericAgent/sandbox/inbox`.
- Move only sandbox-created files into `/opt/GenericAgent/sandbox/trash_review`.
- After a completed long task, let the hidden memory-only settlement broker
  minimally patch verified L1/L2/top-level L3 text or create one top-level L3
  Markdown file. This exception does not apply to the interactive agent.

## Artifact Placement

When a task names a sandbox task directory, write deliverables with absolute
paths under that directory. Do not rely on relative output paths for required
artifacts; GenericAgent may resolve them under `/opt/GenericAgent/temp`
instead of the requested sandbox location.

Before reporting PASS, verify every promised artifact by absolute path.

## Must Ask First

- Interactively patch an existing top-level memory `.md`, `global_mem.txt`, or `global_mem_insight.txt`.
- Interactively create a new top-level memory `.md`.
- Modify any file outside `/opt/GenericAgent/sandbox`.
- Delete, move, or overwrite any real project file.
- Edit `mykey.py` or any file that may contain credentials.
- Send messages, make purchases, change accounts, or mutate external services.
- Install packages or change system configuration unless the user approved the exact action.

## Required User Approval Format

Before writing outside the sandbox, ask for:

```text
我要修改: <absolute path>
动作: <create/edit/delete/move/install>
原因: <why this is necessary>
验证方式: <how I will prove it worked>
是否允许？
```

## Supervisor Rule

If GenericAgent writes outside the sandbox without approval, the supervisor should issue:

```text
_intervene: 停。当前阶段只能写入 /opt/GenericAgent/sandbox。修改真实项目文件前必须请求用户确认。
```

## Enforced Memory Boundary

- `file_patch`: may update an existing top-level `.md` or L1/L2 text store only when `GENERICAGENT_MEMORY_ROOT` is configured.
- `file_write`: may only create a new top-level `.md`; it cannot overwrite, append, or prepend memory.
- `code_run`, web output, nested paths, symlink escapes, `memory/*.py`, JSON, backups, and L4 raw sessions cannot use this memory capability.
- The completion broker receives only `file_read`, `file_patch`, `file_write`,
  and `start_long_term_update`; its output is hidden and its backend history is
  always restored before the user chat session is saved.
- Existing memory patches are atomic and preserve file mode. Secret-like
  content and volatile timestamps/PIDs/session identifiers are rejected, and successful writes emit metadata-only
  `[MEMORY-AUDIT]` journal events.

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |
