#!/usr/bin/env python
"""
Task Watchdog -- Poll a task runner task directory for completion.

CLI:
    python task_watchdog.py <task_dir> [--timeout SECONDS] [--interval SECONDS] [--json]

Exit codes:
    0  completed
    1  error (done.json status=error or unknown status)
    2  timeout (no done.json within timeout)
    3  missing/invalid task dir or invalid done.json

Structured result fields:
    state, task_dir, done_exists, pid, pid_alive, pid_matches_agent,
    elapsed_seconds, status, exit_code, error, input_exists, output_exists,
    stdout_log_exists, stderr_log_exists
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Cross-platform PID cmdline reader (read-only, no os.kill)
# ---------------------------------------------------------------------------

def read_proc_cmdline(pid):
    """Return the command line of a PID as a string, or None."""
    if os.name == 'nt':
        return _read_proc_cmdline_windows(pid)
    return _read_proc_cmdline_linux(pid)


def _read_proc_cmdline_linux(pid):
    """Linux: /proc/<pid>/cmdline."""
    try:
        cmdline_path = f"/proc/{pid}/cmdline"
        if not os.path.isfile(cmdline_path):
            return None
        with open(cmdline_path, "rb") as f:
            raw = f.read()
        return raw.decode("ascii", errors="replace").replace("\0", " ").strip()
    except (OSError, IOError):
        return None


def _read_proc_cmdline_windows(pid):
    """Windows: wmic (available on all systems, no install needed)."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/format:value"],
            capture_output=True, text=True, timeout=5, creationflags=0x08000000 if os.name == 'nt' else 0
        )
        for line in result.stdout.splitlines():
            if line.startswith("CommandLine="):
                return line[len("CommandLine="):].strip()
        return None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def pid_is_alive(pid):
    """Check whether a PID exists."""
    if os.name == 'nt':
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5, creationflags=0x08000000
            )
            return str(pid) in result.stdout
        except (subprocess.TimeoutExpired, OSError):
            return False
    return read_proc_cmdline(pid) is not None


def cmdline_looks_like_agent(cmdline_str):
    """Return True if the cmdline string references agentmain.py."""
    if cmdline_str is None:
        return False
    return "agentmain.py" in cmdline_str


# ---------------------------------------------------------------------------
# JSON safe reader
# ---------------------------------------------------------------------------

def read_json_safe(path):
    """Return (data_dict, error_string). error_string is None on success."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data, None
    except FileNotFoundError:
        return None, "file_not_found"
    except json.JSONDecodeError as exc:
        return None, f"invalid_json: {exc}"


# ---------------------------------------------------------------------------
# Task directory probe
# ---------------------------------------------------------------------------

def probe_task_dir(task_dir_path):
    """Collect structured information about a task directory.

    Returns a dict with all required fields (see module docstring).
    """
    td = Path(task_dir_path)
    info = {
        "task_dir": str(td.resolve()),
        "done_exists": False,
        "pid": None,
        "pid_alive": False,
        "pid_matches_agent": False,
        "status": None,
        "exit_code": None,
        "error": None,
        "input_exists": False,
        "output_exists": False,
        "stdout_log_exists": False,
        "stderr_log_exists": False,
    }

    if not td.is_dir():
        return info

    # ---- done.json ----
    done_path = td / "done.json"
    if done_path.is_file():
        info["done_exists"] = True
        data, err = read_json_safe(str(done_path))
        if err:
            info["error"] = err
        else:
            info["status"] = data.get("status")
            info["exit_code"] = data.get("exit_code")
            info["error"] = data.get("error")

    # ---- pid file ----
    pid_path = td / "pid"
    if pid_path.is_file():
        try:
            raw = pid_path.read_text().strip()
            pid = int(raw)
            info["pid"] = pid
            cmdline = read_proc_cmdline(pid)
            if cmdline is not None:
                info["pid_alive"] = True
                info["pid_matches_agent"] = cmdline_looks_like_agent(cmdline)
        except (ValueError, OSError, IOError):
            pass

    # ---- input / log / output files ----
    info["input_exists"] = (td / "input.txt").is_file()
    info["output_exists"] = (td / "output.txt").is_file()
    info["stdout_log_exists"] = (td / "stdout.log").is_file()
    info["stderr_log_exists"] = (td / "stderr.log").is_file()

    return info


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def determine_state(probe, elapsed, timeout):
    """Return a short string describing the task state."""
    task_dir = probe.get("task_dir", "")
    if not task_dir or not os.path.isdir(task_dir):
        return "missing_task_dir"

    if not probe["done_exists"]:
        if elapsed >= timeout:
            return "timeout"
        return "running"

    # done.json exists
    if probe["error"] and probe["error"].startswith("invalid_json"):
        return "invalid_done_json"

    status = probe.get("status")
    if status == "completed":
        return "completed"
    elif status == "error":
        return "error"
    else:
        return "unknown_status"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

STATE_EXIT_CODES = {
    "completed": 0,
    "error": 1,
    "unknown_status": 1,
    "timeout": 2,
    "invalid_done_json": 3,
    "missing_task_dir": 3,
}


def format_human(probe):
    """Return a human-readable representation of probe results."""
    lines = [
        f"Task Directory:     {probe['task_dir']}",
        f"State:              {probe.get('state', 'unknown')}",
        f"Elapsed:            {probe.get('elapsed_seconds', 0):.1f}s",
        f"done.json exists:   {probe['done_exists']}",
        f"PID:                {probe['pid']}",
        f"PID alive:          {probe['pid_alive']}",
        f"PID matches agent:  {probe['pid_matches_agent']}",
        f"Status:             {probe['status']}",
        f"Exit code:          {probe['exit_code']}",
    ]
    if probe.get("error"):
        lines.append(f"Error:              {probe['error']}")
    lines.extend([
        f"input.txt exists:   {probe['input_exists']}",
        f"output.txt exists:  {probe['output_exists']}",
        f"stdout.log exists:  {probe['stdout_log_exists']}",
        f"stderr.log exists:  {probe['stderr_log_exists']}",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Task Runner Watchdog -- poll done.json and check PID"
    )
    parser.add_argument("task_dir", help="Absolute path to the task directory")
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Max wait in seconds (default 300)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5,
        help="Poll interval in seconds (default 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON instead of human-readable text",
    )
    args = parser.parse_args()

    task_dir = args.task_dir
    timeout = args.timeout
    interval = args.interval
    json_mode = args.json
    start_time = time.time()

    # Early exit if the task directory is missing
    if not os.path.isdir(task_dir):
        probe = probe_task_dir(task_dir)
        probe["elapsed_seconds"] = round(time.time() - start_time, 2)
        probe["state"] = "missing_task_dir"
        _emit_and_exit(probe, json_mode)

    # Polling loop
    while True:
        elapsed = time.time() - start_time
        probe = probe_task_dir(task_dir)
        probe["elapsed_seconds"] = round(elapsed, 2)
        state = determine_state(probe, elapsed, timeout)
        probe["state"] = state

        if state in ("completed", "error", "timeout",
                      "invalid_done_json", "missing_task_dir", "unknown_status"):
            _emit_and_exit(probe, json_mode)

        time.sleep(interval)


def _emit_and_exit(probe, json_mode):
    """Print the result (human or JSON) and exit with the appropriate code."""
    if json_mode:
        print(json.dumps(probe, indent=2, default=str))
    else:
        print(format_human(probe))
    sys.exit(STATE_EXIT_CODES.get(probe.get("state"), 1))


if __name__ == "__main__":
    main()
