#!/usr/bin/env python
"""GenericAgent Configuration Validator — self-diagnostic tool.

Validates: Python env, core imports, mykey.py config, tools schema, disk space.
Run: python config_check.py [--json]

Exit codes: 0=all pass, 1=warnings, 2=errors.
"""
import os, sys, json, argparse, subprocess, tempfile, re
from pathlib import Path

# Detect project root: walk up until we find pyproject.toml
_P = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = _P
for _ in range(5):
    if os.path.isfile(os.path.join(PROJECT_ROOT, 'pyproject.toml')):
        break
    PROJECT_ROOT = os.path.dirname(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)


def load_private_env(path, environ=None):
    """Load simple KEY=VALUE entries without exposing or overriding secrets."""
    target = os.environ if environ is None else environ
    loaded = []
    env_path = Path(path)
    if not env_path.is_file():
        return tuple(loaded)
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name not in target:
            target[name] = value
            loaded.append(name)
    return tuple(loaded)


# systemd reads this file for the service; the standalone validator must do so too.
# Names may be reported by tests, but values are never printed here.
PRIVATE_ENV_NAMES = load_private_env(Path(PROJECT_ROOT) / ".env")

CHECKS = []

def check(name, category="core"):
    """Decorator to register a check function. Returns (name, category, pass/warn/fail, message)."""
    def decorator(func):
        CHECKS.append((name, category, func))
        return func
    return decorator


# ── Python Environment ──────────────────────────────────────────────

@check("Python Version", "env")
def check_python_version():
    import platform
    v = platform.python_version()
    major, minor = int(v.split('.')[0]), int(v.split('.')[1])
    if major >= 3 and minor >= 11:
        return "pass", f"Python {v}"
    return "fail", f"Python {v} — need 3.11+"


@check("Project Root", "env")
def check_project_root():
    if os.path.isdir(PROJECT_ROOT) and os.path.isfile(os.path.join(PROJECT_ROOT, 'pyproject.toml')):
        return "pass", PROJECT_ROOT
    return "fail", f"Not a GenericAgent project: {PROJECT_ROOT}"


# ── Core Imports ─────────────────────────────────────────────────────

@check("agent_loop import", "imports")
def check_agent_loop():
    try:
        import agent_loop
        return "pass", f"agent_loop ({getattr(agent_loop, '__file__', '?')})"
    except ImportError as e:
        return "fail", str(e)


@check("llmcore import", "imports")
def check_llmcore():
    try:
        import llmcore
        return "pass", f"llmcore ({getattr(llmcore, '__file__', '?')})"
    except ImportError as e:
        return "fail", str(e)


@check("ga import", "imports")
def check_ga():
    try:
        import ga
        return "pass", f"ga ({getattr(ga, '__file__', '?')})"
    except ImportError as e:
        return "fail", str(e)


@check("Agent instantiation", "imports")
def check_agent():
    """Instantiate the runtime in an isolated process.

    GenericAgent initialization may start watchdog threads and provider clients.
    Keeping those side effects in the validator process made config_check hang
    after it had already completed its checks.
    """
    code = (
        "from agentmain import GenericAgent\n"
        "agent = GenericAgent(start_watchdog=False)\n"
        "print(type(agent).__name__, flush=True)\n"
        "import os\n"
        "os._exit(0)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=0x08000000 if os.name == 'nt' else 0,
        )
    except subprocess.TimeoutExpired:
        return "fail", "GenericAgent initialization timed out after 20s"
    except Exception as e:
        return "fail", f"Cannot run isolated check: {e}"

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "no output").strip().splitlines()
        return "fail", details[-1][:200]
    name = next((line.strip() for line in reversed(result.stdout.splitlines()) if line.strip()), "GenericAgent")
    return "pass", f"GenericAgent ({name})"


# ── API Key Config ───────────────────────────────────────────────────

@check("mykey.py exists", "config")
def check_mykey_exists():
    path = os.path.join(PROJECT_ROOT, 'mykey.py')
    if os.path.isfile(path):
        stat = os.stat(path)
        return "pass", f"mykey.py ({stat.st_size} bytes)"
    return "fail", "mykey.py not found — copy mykey_template.py to mykey.py"


@check("DEEPSEEK_API_KEY env", "config")
def check_api_key():
    key = os.environ.get('DEEPSEEK_API_KEY', '')
    if key and key.startswith('sk-'):
        return "pass", "DEEPSEEK_API_KEY configured"
    elif key:
        return "warn", "DEEPSEEK_API_KEY set but doesn't start with 'sk-'"
    # Check mykey_local.py
    local_path = os.path.join(PROJECT_ROOT, 'mykey_local.py')
    if os.path.isfile(local_path):
        return "pass", "mykey_local.py exists (local key file)"
    return "warn", "No API key env var and no mykey_local.py"


@check("GENERICAGENT_PROXY env", "config")
def check_proxy():
    proxy = os.environ.get('GENERICAGENT_PROXY', '')
    if proxy:
        return "pass", "GENERICAGENT_PROXY configured"
    return "pass", "Direct network mode (no proxy configured)"


# ── Toolchain ────────────────────────────────────────────────────────

@check("tools_schema.json", "tools")
def check_tools_schema():
    path = os.path.join(PROJECT_ROOT, 'assets', 'tools_schema.json')
    if not os.path.isfile(path):
        return "fail", "tools_schema.json not found"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        names = [t['function']['name'] for t in schema]
        expected = {'code_run', 'file_read', 'file_patch', 'file_write',
                    'web_scan', 'web_execute_js', 'web_search', 'web_fetch',
                    'update_working_checkpoint', 'ask_user', 'start_long_term_update'}
        missing = expected - set(names)
        if missing:
            return "warn", f"{len(names)} tools, missing: {missing}"
        return "pass", f"{len(names)} tools registered"
    except Exception as e:
        return "fail", str(e)


@check("sys_prompt.txt", "tools")
def check_sys_prompt():
    path = os.path.join(PROJECT_ROOT, 'assets', 'sys_prompt.txt')
    if os.path.isfile(path):
        return "pass", f"sys_prompt.txt ({os.stat(path).st_size} bytes)"
    return "fail", "sys_prompt.txt not found"


@check("Web connectivity", "tools")
def check_web():
    try:
        import urllib.request
        req = urllib.request.Request('https://example.com', headers={
            'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return "pass", "HTTP 200 — example.com reachable"
            return "warn", f"HTTP {resp.status}"
    except Exception as e:
        return "warn", f"Cannot reach example.com: {str(e)[:80]}"


@check("Web search (Bing)", "tools")
def check_web_search():
    try:
        from ga import web_search
        r = web_search("test", max_results=1)
        if r.get("status") == "success" and r.get("results"):
            backend = r.get("backend", "?")
            return "pass", f"Working (backend: {backend}, {len(r['results'])} results)"
        return "warn", f"No results — {r.get('msg', 'unknown error')[:100]}"
    except Exception as e:
        return "warn", str(e)[:100]


@check("code_run sandbox", "tools")
def check_code_run_sandbox():
    if os.name == "nt":
        return "warn", "Bubblewrap integration is Linux-only"
    try:
        from ga import code_run

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed"
            outside = root / "outside.txt"
            allowed.mkdir()
            old_roots = os.environ.get("GENERICAGENT_WRITE_ROOTS")
            os.environ["GENERICAGENT_WRITE_ROOTS"] = str(allowed)
            try:
                runner = code_run(
                    f"printf denied > {outside}",
                    "bash",
                    timeout=10,
                    cwd=str(root),
                    myprint=lambda *args, **kwargs: None,
                )
                while True:
                    try:
                        next(runner)
                    except StopIteration as stopped:
                        result = stopped.value
                        break
            finally:
                if old_roots is None:
                    os.environ.pop("GENERICAGENT_WRITE_ROOTS", None)
                else:
                    os.environ["GENERICAGENT_WRITE_ROOTS"] = old_roots
            if outside.exists():
                return "fail", "Bubblewrap allowed a write outside configured roots"
            if result.get("status") != "error":
                return "fail", f"Write denial returned {result}"
        return "pass", "Bubblewrap denied outside-root write"
    except Exception as e:
        return "fail", str(e)[:160]


# ── Memory System ────────────────────────────────────────────────────

@check("Memory index (L1)", "memory")
def check_l1():
    path = os.path.join(PROJECT_ROOT, 'memory', 'global_mem_insight.txt')
    if not os.path.isfile(path):
        return "fail", "L1 index not found"
    with open(path, 'r', encoding='utf-8') as f:
        lines = [l for l in f.readlines() if l.strip()]
    n = len(lines)
    if n <= 30:
        return "pass", f"L1: {n} lines (≤30 limit)"
    return "warn", f"L1: {n} lines (exceeds 30-line limit)"


@check("SOP inventory", "memory")
def check_sops():
    mem_dir = os.path.join(PROJECT_ROOT, 'memory')
    sops = [f for f in os.listdir(mem_dir) if f.endswith('_sop.md')]
    return "pass", f"{len(sops)} SOPs"


@check("L4 archive", "memory")
def check_l4():
    path = os.path.join(PROJECT_ROOT, 'memory', 'L4_raw_sessions', 'all_histories.txt')
    if os.path.isfile(path):
        kb = os.stat(path).st_size / 1024
        return "pass", f"L4 archive: {kb:.1f} KB"
    return "warn", "L4 all_histories.txt not found"


# ── System ───────────────────────────────────────────────────────────

@check("Disk space", "system")
def check_disk():
    try:
        import shutil
        usage = shutil.disk_usage(PROJECT_ROOT)
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        pct = usage.free / usage.total * 100
        drive = os.path.splitdrive(PROJECT_ROOT)[0] or '/'
        if pct > 20:
            return "pass", f"{drive} {free_gb:.1f}GB free / {total_gb:.1f}GB ({pct:.1f}%)"
        elif pct > 10:
            return "warn", f"{drive} only {free_gb:.1f}GB free ({pct:.1f}%)"
        return "fail", f"{drive} CRITICAL: {free_gb:.1f}GB free ({pct:.1f}%)"
    except Exception as e:
        return "warn", f"Disk check failed: {e}"


@check("Git status", "system")
def check_git():
    try:
        import subprocess
        r = subprocess.run(
            ["git", "log", "--oneline", "-3"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=10,
            creationflags=0x08000000 if os.name == 'nt' else 0
        )
        if r.returncode == 0:
            return "pass", f"Git OK — {r.stdout.splitlines()[0] if r.stdout.strip() else 'no commits'}"
        return "warn", "Git error"
    except Exception:
        return "warn", "Git not available"


# ── Report ───────────────────────────────────────────────────────────

def run_all():
    results = []
    for name, category, func in CHECKS:
        try:
            status, msg = func()
        except Exception as e:
            status, msg = "fail", f"Exception: {e}"
        results.append((name, category, status, msg))
    return results


def format_report(results, json_mode=False):
    if json_mode:
        out = []
        for name, cat, status, msg in results:
            out.append({"check": name, "category": cat, "status": status, "message": msg})
        return json.dumps(out, indent=2, ensure_ascii=False)

    lines = []
    lines.append("=" * 65)
    lines.append("  GenericAgent Configuration Validator")
    lines.append("=" * 65)

    categories = {}
    for name, cat, status, msg in results:
        categories.setdefault(cat, []).append((name, status, msg))

    for cat in ["env", "imports", "config", "tools", "memory", "system"]:
        items = categories.get(cat, [])
        cat_names = {"env": "Environment", "imports": "Core Imports", "config": "API Config",
                     "tools": "Toolchain", "memory": "Memory System", "system": "System"}
        lines.append(f"\n── {cat_names.get(cat, cat)} ──")
        for name, status, msg in items:
            icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(status, "?")
            lines.append(f"  {icon} {name}: {msg}")

    passes = sum(1 for _, _, s, _ in results if s == "pass")
    warns = sum(1 for _, _, s, _ in results if s == "warn")
    fails = sum(1 for _, _, s, _ in results if s == "fail")

    lines.append(f"\n{'=' * 65}")
    lines.append(f"  Summary: {passes} pass, {warns} warn, {fails} fail")
    if fails > 0:
        lines.append(f"  VERDICT: ❌ NOT READY ({fails} failures)")
    elif warns > 0:
        lines.append(f"  VERDICT: ⚠️ READY with warnings")
    else:
        lines.append(f"  VERDICT: ✅ FULLY READY")
    lines.append(f"{'=' * 65}")

    return "\n".join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='GenericAgent Config Validator')
    parser.add_argument('--json', action='store_true', help='JSON output')
    args = parser.parse_args()

    results = run_all()
    print(format_report(results, json_mode=args.json))

    fails = sum(1 for _, _, s, _ in results if s == "fail")
    sys.exit(2 if fails > 0 else (1 if any(s == "warn" for _, _, s, _ in results) else 0))
