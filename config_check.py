#!/usr/bin/env python
"""GenericAgent Configuration Validator — self-diagnostic tool.

Validates: Python env, core imports, mykey.py config, tools schema, disk space.
Run: python config_check.py [--json]

Exit codes: 0=all pass, 1=warnings, 2=errors.
"""
import os, sys, json, argparse

# Detect project root: walk up until we find pyproject.toml
_P = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = _P
for _ in range(5):
    if os.path.isfile(os.path.join(PROJECT_ROOT, 'pyproject.toml')):
        break
    PROJECT_ROOT = os.path.dirname(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

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
    try:
        from agentmain import GenericAgent
        a = GenericAgent()
        return "pass", f"GenericAgent ({type(a).__name__})"
    except Exception as e:
        return "fail", str(e)


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
        return "pass", f"DEEPSEEK_API_KEY set (masked: sk-...{key[-4:]})"
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
        return "pass", proxy
    return "warn", "No proxy set — API calls may fail on restricted networks"


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
