# Sandbox Policy

## Current Mode

GenericAgent is in cold-start supervised mode.

Real project directories are read-only by default. The only pre-approved write location is:

```text
/opt/GenericAgent/sandbox
```

## Allowed Without Asking

- Read files under `/opt/GenericAgent`.
- Create drafts under `/opt/GenericAgent/sandbox/workspace`.
- Create reports under `/opt/GenericAgent/sandbox/reports`.
- Copy user-provided samples into `/opt/GenericAgent/sandbox/inbox`.
- Move only sandbox-created files into `/opt/GenericAgent/sandbox/trash_review`.

## Artifact Placement

When a task names a sandbox task directory, write deliverables with absolute
paths under that directory. Do not rely on relative output paths for required
artifacts; GenericAgent may resolve them under `/opt/GenericAgent/temp`
instead of the requested sandbox location.

Before reporting PASS, verify every promised artifact by absolute path.

## Must Ask First

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

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |

