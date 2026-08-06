# Task Runner SOP

**Purpose:** How to use `agentmain.py --task` for background/foreground task execution
**Status:** Formal -- replaces `task_runner_usage_sop_draft_v2.md`

---

## 0. WARNING Input Security Rules

> Command-line arguments are world-readable via `ps aux` / `/proc/<pid>/cmdline`.

### 0.1 Prefer `input.txt` over `--input`

| Scenario | Method | Risk |
|----------|--------|------|
| Long prompt (>200 chars) | Write to `task_dir/input.txt` | Safe |
| Sensitive prompt (key/token/path/biz) | Write to `task_dir/input.txt` | Safe |
| Production / supervisor tasks | Write to `task_dir/input.txt` | Safe |
| Short non-sensitive debug (<=200 chars) | `--input` allowed | Low |

```bash
# CORRECT -- write prompt to file, no cmdline exposure
printf '%s\n' "long prompt..." > /opt/GenericAgent/temp/task_dir/input.txt
python3 agentmain.py --task /opt/GenericAgent/temp/task_dir --once

# WRONG -- prompt leaks to cmdline
python3 agentmain.py --task /opt/GenericAgent/temp/task_dir --input "long sensitive prompt..." --once
```

---

## 1. Quick Reference

```bash
# Supervisor Mode (background Popen): writes pid/stdout.log/stderr.log/done.json
printf '%s\n' "prompt" > input.txt && python3 agentmain.py --task /opt/GenericAgent/temp --once

# Debug Mode (foreground inline): stdout + output.txt + done.json
printf '%s\n' "prompt" > input.txt && python3 agentmain.py --task /opt/GenericAgent/temp --nobg --once

# --input shortcut (<=200 chars, no secrets, one-off only)
python3 agentmain.py --task /opt/GenericAgent/temp --input "short" --nobg --once
```

---

## 2. Path Resolution (`resolve_task_dir()`)

| Input Pattern | Example | Resolves To |
|---------------|---------|-------------|
| Absolute path | `/data/temp/my_task` | `/data/temp/my_task` (passed as-is) |
| Relative (./ or ../) | `./tasks/x` | Relative to `agentmain.py` dir |
| Simple name | `my_task` | `<script_dir>/temp/my_task` |
| Nested path | `a/b/c` | `<script_dir>/temp/a/b/c` |
| Empty / whitespace | `""` / `"   "` | **ValueError** (rejected) |

> **Always use absolute paths for supervisor/automated tasks** -- no ambiguity.

### Output Placement Rule

When a task asks for deliverables in a sandbox task directory, write each artifact
with its absolute path there. Relative `file_write` paths are resolved under
GenericAgent's default working area, commonly `<script_dir>/temp/`, and will fail
artifact-location acceptance.

Before claiming completion, inventory the requested absolute paths:

```bash
for f in expected_a.md expected_b.md; do
    if [ ! -f "$TASK_DIR/$f" ]; then echo "MISSING: $TASK_DIR/$f"; fi
done
```

---

## 3. Task Lifecycle (`--once`)

**Background** (default, no `--nobg`): Creates `task_dir/`, writes PID to `pid`, captures stdout->`stdout.log`, stderr->`stderr.log`, on completion writes `done.json`+`output.txt`, child exits.

**Foreground** (`--nobg --once`): Runs inline, no subprocess, stdout direct, writes `output.txt`+`done.json`, exits after one LLM round.

### Lifecycle Files

| File | Purpose | Written By |
|------|---------|-----------|
| `pid` | Child process ID | Supervisor (bg only) |
| `stdout.log` | Captured stdout | Background subprocess |
| `stderr.log` | Captured stderr | Background subprocess |
| `input.txt` | Task prompt input | User (you write this) |
| `output.txt` | Agent response output | Agent on completion |
| `done.json` | Completion signal + status | Agent on completion |
| `_stop` | Sentinel for graceful abort | User (touch to stop) |

---

## 4. Completion Detection

**Only poll `done.json`** -- do NOT rely on process exit.

```bash
if [ -f "$TASK_DIR/done.json" ]; then
    python3 - <<'PY'
import json, os
d = json.load(open(os.path.join(os.environ['TASK_DIR'], 'done.json')))
print(f'Status: {d["status"]}, Exit: {d["exit_code"]}')
PY
fi
```

Schema: `{"status":"completed","exit_code":0}` or `{"status":"error","exit_code":1,"error":"input.txt not found at ..."}`

Poll loop: `until [ -f "$TASK_DIR/done.json" ]; do sleep 5; done; cat "$TASK_DIR/output.txt"`

---

## 5. Failure Diagnosis

| Step | Command | What to Look For |
|------|---------|-----------------|
| 1 | `cat "$TASK_DIR/done.json" \| python3 -m json.tool` | `status`, `exit_code`, `error` field |
| 2 | `cat "$TASK_DIR/stderr.log"` | Python traceback, API errors |
| 3 | `cat "$TASK_DIR/stdout.log"` | Progress messages, last output |
| 4 | `ls -la "$TASK_DIR/input.txt"` | Missing input? Empty file? |

Common errors: `"input.txt not found"` -> missing input; LLM exception -> check API key/network.

---

## 6. Prohibited Patterns

| Anti-pattern | Why |
|-------------|-----|
| `python3 agentmain.py --task ... &`（裸后台启动，不写 pid） | No PID tracking, no log capture, orphan risk |
| `kill $(cat pid)` without verifying | May kill unrelated process |
| `os.kill()` in Python | Banned |
| Unconditional `pkill -f python` | Kills the supervisor itself |
| `--input` for long/sensitive prompts | Content leaks via cmdline |
| Relative paths for requested artifacts | May write to `<script_dir>/temp/` instead of the sandbox task directory |

---

## 7. Correct Cleanup

```bash
# Verify PID belongs to agentmain.py before killing
TASK_DIR="/opt/GenericAgent/temp/task_dir"
pid=$(cat "$TASK_DIR/pid" 2>/dev/null)
if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1 &&
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'agentmain'; then
    kill "$pid"
fi

# Or use _stop sentinel for graceful abort
touch "$TASK_DIR/_stop"
```

---

## 8. Post-Spawn Verification

```bash
TASK_DIR="/opt/GenericAgent/temp/task_dir"
python3 /opt/GenericAgent/memory/task_watchdog.py "$TASK_DIR" --timeout 300 --interval 5 --json

# Security: verify no prompt in cmdline
if [ -n "$pid" ]; then
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- '--input'; then
        echo "WARNING: --input used, prompt visible"
    fi
fi
```

Use formal watchdog as primary gate; hand-written `ps`/`grep` is secondary.

---

## Appendix: Security Rationale

Process command lines are world-readable. Any user, monitoring tool, CI pipeline, or audit framework can see `--input` values. Writing to `input.txt` keeps content off the command line:

```
python3 agentmain.py --task /opt/GenericAgent/temp/task_dir --once
```

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |
| v3 | 2026-08-06 | 迁移至 Linux bash（Ubuntu 24.04，root）：命令、路径、生命周期文件示例全部 Linux 化 |

