# Task Runner SOP

**Purpose:** How to use `agentmain.py --task` for background/foreground task execution
**Status:** Formal -- replaces `task_runner_usage_sop_draft_v2.md`

---

## 0. WARNING Input Security Rules

> Command-line arguments are world-readable via `ps aux` / `ps aux`.

### 0.1 Prefer `input.txt` over `--input`

| Scenario | Method | Risk |
|----------|--------|------|
| Long prompt (>200 chars) | Write to `task_dir/input.txt` | Safe |
| Sensitive prompt (key/token/path/biz) | Write to `task_dir/input.txt` | Safe |
| Production / supervisor tasks | Write to `task_dir/input.txt` | Safe |
| Short non-sensitive debug (<=200 chars) | `--input` allowed | Low |

```powershell
# CORRECT -- write prompt to file, no cmdline exposure
Set-Content -Path /opt/GenericAgent/temp\task_dir\input.txt -Value "long prompt..."
python agentmain.py --task /opt/GenericAgent/temp\task_dir --once

# WRONG -- prompt leaks to cmdline
python agentmain.py --task /opt/GenericAgent/temp\task_dir --input "long sensitive prompt..." --once
```

---

## 1. Quick Reference

```powershell
# Supervisor Mode (background Popen): writes pid/stdout.log/stderr.log/done.json
Set-Content -Path input.txt -Value "prompt" | python agentmain.py --task /opt/GenericAgent/temp --once

# Debug Mode (foreground inline): stdout + output.txt + done.json
Set-Content -Path input.txt -Value "prompt" | python agentmain.py --task /opt/GenericAgent/temp --nobg --once

# --input shortcut (<=200 chars, no secrets, one-off only)
python agentmain.py --task /opt/GenericAgent/temp --input "short" --nobg --once
```

---

## 2. Path Resolution (`resolve_task_dir()`)

| Input Pattern | Example | Resolves To |
|---------------|---------|-------------|
| Absolute path | `D:\temp\my_task` | `D:\temp\my_task` (passed as-is) |
| Relative (./ or ../) | `.\tasks\x` | Relative to `agentmain.py` dir |
| Simple name | `my_task` | `<script_dir>\temp\my_task` |
| Nested path | `a\b\c` | `<script_dir>\temp\a\b\c` |
| Empty / whitespace | `""` / `"   "` | **ValueError** (rejected) |

> **Always use absolute paths for supervisor/automated tasks** -- no ambiguity.

### Output Placement Rule

When a task asks for deliverables in a sandbox task directory, write each artifact
with its absolute path there. Relative `file_write` paths are resolved under
GenericAgent's default working area, commonly `<script_dir>\temp\`, and will fail
artifact-location acceptance.

Before claiming completion, inventory the requested absolute paths:

```powershell
$expected = @('expected_a.md', 'expected_b.md')
foreach ($f in $expected) {
    if (-not (Test-Path "$env:TASK_DIR\$f")) { Write-Warning "MISSING: $env:TASK_DIR\$f" }
}
```

---

## 3. Task Lifecycle (`--once`)

**Background** (default, no `--nobg`): Creates `task_dir\`, writes PID to `pid`, captures stdout->`stdout.log`, stderr->`stderr.log`, on completion writes `done.json`+`output.txt`, child exits.

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

```powershell
if (Test-Path "$env:TASK_DIR\done.json") {
    python -c @'
import json
d = json.load(open(r'$env:TASK_DIR\done.json'))
print(f'Status: {d["status"]}, Exit: {d["exit_code"]}')
'@
}
```

Schema: `{"status":"completed","exit_code":0}` or `{"status":"error","exit_code":1,"error":"input.txt not found at ..."}`

Poll loop: `while (-not (Test-Path "$env:TASK_DIR\done.json")) { Start-Sleep -Seconds 5 } ; Get-Content "$env:TASK_DIR\output.txt"`

---

## 5. Failure Diagnosis

| Step | Command | What to Look For |
|------|---------|-----------------|
| 1 | `Get-Content "$env:TASK_DIR\done.json" \| ConvertFrom-Json` | `status`, `exit_code`, `error` field |
| 2 | `Get-Content "$env:TASK_DIR\stderr.log"` | Python traceback, API errors |
| 3 | `Get-Content "$env:TASK_DIR\stdout.log"` | Progress messages, last output |
| 4 | `Test-Path "$env:TASK_DIR\input.txt"` | Missing input? Empty file? |

Common errors: `"input.txt not found"` -> missing input; LLM exception -> check API key/network.

---

## 6. Prohibited Patterns

| Anti-pattern | Why |
|-------------|-----|
| `Start-Process python -ArgumentList 'agentmain.py' -NoNewWindow` | No PID tracking, no log capture, orphan risk |
| `Stop-Process -Id (Get-Content pid)` without verifying | May kill unrelated process |
| `os.kill()` in Python | Banned |
| Unconditional `Stop-Process -Name python*` | Kills the supervisor itself |
| `--input` for long/sensitive prompts | Content leaks via cmdline |
| Relative paths for requested artifacts | May write to `<script_dir>\temp\` instead of the sandbox task directory |

---

## 7. Correct Cleanup

```powershell
# Verify PID belongs to agentmain.py before killing
$taskDir = "/opt/GenericAgent/temp\task_dir"
$pid = Get-Content "$taskDir\pid" 2>/dev/null
if ($pid -and (ps aux | grep -Id $pid 2>/dev/null) -and 
    (ps aux -Filter "ProcessId=$pid" | Where-Object CommandLine -match 'agentmain')) {
    Stop-Process -Id $pid
}

# Or use _stop sentinel for graceful abort
Set-Content -Path "$taskDir\_stop" -Value ""
```

---

## 8. Post-Spawn Verification

```powershell
$taskDir = "/opt/GenericAgent/temp\task_dir"
python /opt/GenericAgent/memory\task_watchdog.py "$taskDir" --timeout 300 --interval 5 --json

# Security: verify no prompt in cmdline
if ($pid) {
    $cmdline = (ps aux -Filter "ProcessId=$pid").CommandLine
    if ($cmdline -match '--input') { Write-Warning "--input used, prompt visible" }
}
```

Use formal watchdog as primary gate; hand-written `ps`/`grep` is secondary.

---

## Appendix: Security Rationale

Process command lines are world-readable. Any user, monitoring tool, CI pipeline, or audit framework can see `--input` values. Writing to `input.txt` keeps content off the command line:

```
python agentmain.py --task /opt/GenericAgent/temp\task_dir --once
```

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |

