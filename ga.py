import gc
import sys, os, re, json, time, threading, importlib
from datetime import datetime
from pathlib import Path, PureWindowsPath
import tempfile, traceback, subprocess, itertools, collections, difflib, shutil
import urllib.request, urllib.parse, urllib.error, html as _html
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agent_loop import BaseHandler, StepOutcome, json_default
from memory_policy import validate_injected_memory, validate_memory_content
script_dir = os.path.dirname(os.path.abspath(__file__))

# ── Dangerous command patterns for guardrails ──
_DANGEROUS_PATTERNS = [
    # rm -rf 全变体（-rf / -fr / -rfv / --recursive --force 任意组合）作用于根或根级系统目录。
    # 原规则要求 /\w+ 后有空白，`rm -rf /etc`（结尾无空格）可绕过——已补锚点。
    (r'\brm\s+(?:-[a-z]*[rf][a-z]*[rf][a-z]*|--(?:recursive|force)(?:\s+--(?:recursive|force))?)\s+/\s*$', 'rm -rf / (dangerous deletion)'),
    (r'\brm\s+(?:-[a-z]*[rf][a-z]*[rf][a-z]*|--(?:recursive|force)(?:\s+--(?:recursive|force))?)\s+/\*', 'rm -rf /* (dangerous deletion)'),
    (r'\brm\s+(?:-[a-z]*[rf][a-z]*[rf][a-z]*|--(?:recursive|force)(?:\s+--(?:recursive|force))?)\s+/(?:etc|bin|sbin|usr|var|boot|lib|lib64|opt|root|home|run|dev|proc|sys|srv|mnt|media)\b', 'rm -rf on root-level system directory (dangerous)'),
    (r'\b(?:dd|format)\s+', 'disk destroyer command'),
    (r'\bchmod\s+777\s+/', 'chmod 777 on root'),
    (r'>\s*/dev/(?!null)', 'writing to system device'),
    (r'\bmkfs\.', 'filesystem formatting'),
    (r'\bshutdown\b|\breboot\b|\bpoweroff\b', 'system shutdown'),
    (r'\b:\(\)\s*\{', 'fork bomb'),
    (r'\bwget\s+.*\|\s*bash\b', 'pipe-to-bash'),
    (r'\bcurl\s+.*\|\s*bash\b', 'pipe-to-bash'),
]

_ENABLED_VALUES = {"1", "true", "yes", "on"}
_MCP_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _env_flag_enabled(name):
    return os.environ.get(name, "").strip().lower() in _ENABLED_VALUES


def _mcp_tool_allowed(server, tool):
    if not isinstance(server, str) or not isinstance(tool, str):
        return False
    if not _MCP_IDENTIFIER_RE.fullmatch(server) or not _MCP_IDENTIFIER_RE.fullmatch(tool):
        return False
    raw_allowlist = os.environ.get("GENERICAGENT_MCP_ALLOWLIST", "")
    for entry in re.split(r"[,;\s]+", raw_allowlist.strip()):
        if not entry or "/" not in entry:
            continue
        allowed_server, allowed_tool = entry.split("/", 1)
        if not _MCP_IDENTIFIER_RE.fullmatch(allowed_server):
            continue
        if allowed_tool != "*" and not _MCP_IDENTIFIER_RE.fullmatch(allowed_tool):
            continue
        if server == allowed_server and (tool == allowed_tool or allowed_tool == "*"):
            return True
    return False


def _check_dangerous_command(code):
    """Check code for dangerous patterns. Returns (is_dangerous, reason)."""
    for pat, desc in _DANGEROUS_PATTERNS:
        if re.search(pat, code):
            return True, desc
    return False, ''


def _sandbox_enabled():
    """Global code_run sandbox switch.

    GENERICAGENT_SANDBOX=0 disables bubblewrap for every code_run call
    (host execution everywhere). Per-call ``mode: "host"`` still works and
    is audited regardless of this switch.
    """
    return os.environ.get("GENERICAGENT_SANDBOX", "1").lower() not in (
        "0", "false", "no", "off",
    )


def _audit_admin_op(code, source):
    """Append an audit trail for unsandboxed (host-mode) code_run executions."""
    try:
        os.makedirs(os.path.join(script_dir, "temp"), exist_ok=True)
        with open(
            os.path.join(script_dir, "temp", "admin_ops.log"),
            "a", encoding="utf-8",
        ) as f:
            f.write(
                f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] '
                f'source={source} cmd={code[:800]}\n'
            )
    except Exception:
        pass

DEFAULT_WRITE_ROOT = os.path.join(script_dir, 'sandbox')
DEFAULT_EXECUTION_ROOT = os.path.join(script_dir, 'temp')
DEFAULT_IPC_PRIVATE_ROOT = "/run/genericagent"
_CODE_READ_DIR_NAMES = (
    "assets", "docs", "frontends", "memory", "reflect", "sche_tasks",
    "scripts", "tests",
)
_CODE_READ_FILE_NAMES = (
    "agent_loop.py", "agentmain.py", "change_approval.py", "ga.py",
)
_PRIVATE_KEY_NAMES = {
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "id_xmss",
}
_PRIVATE_KEY_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


def _resolved_path(path):
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser().absolute()


def _path_within(path, root):
    return path == root or root in path.parents


def _configured_private_roots():
    from change_approval import get_change_state_root
    return tuple(dict.fromkeys((
        get_change_state_root(),
        _resolved_path(
            os.environ.get("GENERICAGENT_IPC_PRIVATE_ROOT", "").strip()
            or DEFAULT_IPC_PRIVATE_ROOT
        ),
    )))


def _private_runtime_path(path):
    target = _resolved_path(path)
    return any(_path_within(target, root) for root in _configured_private_roots())


def _sensitive_path(path):
    """Match path-name based secrets without opening or inspecting content."""
    target = _resolved_path(path)
    name = target.name.lower()
    return (
        ".ssh" in {part.lower() for part in target.parts}
        or name in _PRIVATE_KEY_NAMES
        or name in {"mykey.py", "mykey.json", "authorized_keys"}
        or name.startswith(".env")
        or target.suffix.lower() in _PRIVATE_KEY_SUFFIXES
    )


def _read_allowed(path):
    """Fail closed for server-private, OS-private, and secret-bearing paths."""
    target = _resolved_path(path)
    if _private_runtime_path(target):
        return False
    if os.name != "nt":
        for root in (Path("/proc"), Path("/sys"), Path("/dev")):
            if _path_within(target, root):
                return False
    if _sensitive_path(target):
        return False
    return True


def _read_denied_result(path):
    return {"status": "error", "msg": f"Read denied by private-path policy: {path}"}

def _configured_write_roots():
    """Return absolute directories where agent file-write tools may write."""
    raw = os.environ.get('GENERICAGENT_WRITE_ROOTS', '')
    roots = [DEFAULT_WRITE_ROOT]
    if raw:
        for item in re.split(r'[;,]' + '|' + re.escape(os.pathsep), raw):
            item = item.strip()
            if item:
                roots.append(item)
    resolved = []
    for root in roots:
        try:
            resolved.append(Path(root).expanduser().resolve())
        except OSError:
            resolved.append(Path(root).expanduser().absolute())
    return tuple(dict.fromkeys(resolved))

def _write_allowed(path):
    target = _resolved_path(path)
    if not _read_allowed(target):
        return False
    for root in _configured_write_roots():
        if target == root or root in target.parents:
            return True
    return False


def _execution_mount_root_allowed(path):
    """Reject broad or OS-private roots even if configuration names them."""
    target = _resolved_path(path)
    if target.parent == target or target == _resolved_path(script_dir):
        return False
    if os.name != "nt":
        for private_root in (Path("/etc"), Path("/root"), Path("/run")):
            if _path_within(target, private_root):
                return False
    return not _private_runtime_path(target) and not _sensitive_path(target)


def _ephemeral_execution_root(path):
    """Recognize a process-owned private subtree below the system temp dir."""
    target = _resolved_path(path)
    temp_root = _resolved_path(tempfile.gettempdir())
    if target == temp_root or not _path_within(target, temp_root):
        return False
    try:
        relative = target.relative_to(temp_root)
        private_root = temp_root / relative.parts[0]
        if private_root.is_symlink() or not private_root.is_dir():
            return False
        if os.name != "nt":
            info = private_root.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return False
    except (IndexError, OSError, ValueError):
        return False
    return True


def _execution_root_allowed(path):
    """Allow code only in configured, agent-temp, or private ephemeral trees."""
    target = _resolved_path(path)
    if (
        not target.is_dir()
        or not _read_allowed(target)
        or not _execution_mount_root_allowed(target)
    ):
        return False
    if any(
        _execution_mount_root_allowed(root) and _path_within(target, root)
        for root in _configured_write_roots()
    ):
        return True
    return (
        _path_within(target, _resolved_path(DEFAULT_EXECUTION_ROOT))
        or _ephemeral_execution_root(target)
    )


def _configured_code_read_roots():
    """Return curated application trees for read-only code_run mounts."""
    project = _resolved_path(script_dir)
    roots = []
    for name in _CODE_READ_DIR_NAMES:
        candidate = _resolved_path(project / name)
        if (
            candidate.is_dir()
            and _path_within(candidate, project)
            and candidate != project
            and _execution_mount_root_allowed(candidate)
            and _read_allowed(candidate)
        ):
            roots.append(candidate)
    return tuple(dict.fromkeys(roots))


def _configured_code_read_files():
    """Expose selected top-level source files without mounting project root."""
    project = _resolved_path(script_dir)
    files = []
    for name in _CODE_READ_FILE_NAMES:
        candidate = _resolved_path(project / name)
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and candidate.parent == project
            and _read_allowed(candidate)
        ):
            files.append(candidate)
    return tuple(dict.fromkeys(files))


def _resolve_code_run_cwd(handler_cwd, requested_cwd):
    """Resolve a model-supplied relative cwd without allowing root escape."""
    if requested_cwd is None:
        requested_cwd = "./"
    if not isinstance(requested_cwd, str) or not requested_cwd.strip():
        raise ValueError("code_run cwd must be a non-empty relative path")
    requested = Path(requested_cwd)
    if requested.is_absolute() or PureWindowsPath(requested_cwd).is_absolute():
        raise ValueError("code_run cwd must be relative to the trusted workspace")

    trusted_root = _resolved_path(handler_cwd)
    if not _execution_root_allowed(trusted_root):
        raise PermissionError("code_run handler workspace is not an allowed execution root")
    target = _resolved_path(trusted_root / requested)
    if not _path_within(target, trusted_root):
        raise PermissionError("code_run cwd escapes the trusted workspace")
    if not target.is_dir() or not _read_allowed(target):
        raise PermissionError("code_run cwd is not an allowed workspace directory")
    return trusted_root, target


def _configured_memory_root():
    """Return the explicitly enabled content-memory root, if any."""
    raw = os.environ.get('GENERICAGENT_MEMORY_ROOT', '').strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return Path(raw).expanduser().absolute()


def _memory_content_allowed(path):
    """Allow only top-level Markdown plus the L1/L2 text stores.

    Memory Python, JSON, backups, nested directories, and symlink escapes are
    intentionally excluded.  This is a narrower capability than a normal
    write root and is used only by file_patch / create-only file_write.
    """
    root = _configured_memory_root()
    if root is None:
        return False
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        target = Path(path).expanduser().absolute()
    if target.parent != root:
        return False
    return target.suffix.lower() == '.md' or target.name in {
        'global_mem.txt',
        'global_mem_insight.txt',
    }


def _memory_patch_allowed(path):
    return _memory_content_allowed(path) and Path(path).is_file()


def _memory_create_allowed(path):
    target = Path(path)
    return (
        _memory_content_allowed(path)
        and target.suffix.lower() == '.md'
        and not target.exists()
    )


_MEMORY_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*[^\s#]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)
_MEMORY_VOLATILE_PATTERNS = (
    re.compile(r"(?:验证时间|记录时间|更新时间|当前时间|timestamp)\s*[:：=]\s*\d{4}-\d{2}-\d{2}", re.IGNORECASE),
    re.compile(r"(?:pid|进程号|session[_ -]?id|会话ID)\s*[:：=]\s*[A-Za-z0-9_-]+", re.IGNORECASE),
)


def _contains_sensitive_memory(content):
    text = str(content or "")
    return any(pattern.search(text) for pattern in _MEMORY_SECRET_PATTERNS)


def _memory_rejection_reason(content):
    if _contains_sensitive_memory(content):
        return "secret-like content detected"
    text = str(content or "")
    if any(pattern.search(text) for pattern in _MEMORY_VOLATILE_PATTERNS):
        return "volatile timestamp, PID, or session identifier detected"
    return ""


_CODE_RUN_WRITE_DENIAL_RE = re.compile(
    r"read-only file system|permission denied|write denied outside allowed roots",
    re.IGNORECASE,
)
_CODE_RUN_MEMORY_REFERENCE_RE = re.compile(
    r"(?:\bMEMORY(?:_DIR|_ROOT)?\b|(?:^|[./\\'\"])memory[/\\'\"])",
    re.IGNORECASE,
)


def _code_run_memory_recovery_hint(code, result):
    """Explain the controlled memory route after a sandbox write refusal."""
    if not isinstance(result, dict) or result.get("status") != "error":
        return ""
    output = "\n".join(
        str(result.get(key, "")) for key in ("stdout", "stderr", "msg")
    )
    if not _CODE_RUN_WRITE_DENIAL_RE.search(output):
        return ""
    if not _CODE_RUN_MEMORY_REFERENCE_RE.search(str(code or "")):
        return ""
    return (
        "code_run 的只读错误只代表代码执行沙箱拒绝该写入，不能说明整个记忆系统只读。"
        "若用户当前消息已明确批准准确的记忆修改：先用 file_read 读取；已有顶层 memory .md "
        "或 L1/L2 文本改用 file_patch，新建顶层 .md 改用 file_write。"
        "删除/移动、覆盖或追加已有记忆、.py/JSON/备份/L4/嵌套路径仍受保护；"
        "这类操作应准确报告为需要管理员处理，不要泛化为所有记忆都不可写。"
    )


def _atomic_memory_write(path, content):
    """Replace one memory text file atomically while preserving its mode."""
    target = Path(path)
    mode = target.stat().st_mode & 0o777
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(target.parent),
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, target)
        temp_name = None
    finally:
        if temp_name:
            try: os.unlink(temp_name)
            except FileNotFoundError: pass


def _audit_memory_write(action, path):
    print(f"[MEMORY-AUDIT] action={action} path={Path(path).name}")


def _write_denied_result(path):
    roots = ', '.join(str(r) for r in _configured_write_roots())
    return {
        "status": "error",
        "msg": f"Write denied outside allowed roots: {path}. Allowed roots: {roots}",
    }


def _known_sensitive_mounts(*roots):
    """Enumerate sensitive path names recursively without reading file content."""
    masked_directories = []
    masked_files = []
    skip_directories = {
        "venv", ".venv", "node_modules", "__pycache__", ".tox", ".nox",
    }
    pending = list(dict.fromkeys(_resolved_path(item) for item in roots))
    visited = set()
    while pending:
        directory = pending.pop()
        if directory in visited:
            continue
        visited.add(directory)
        try:
            children = tuple(directory.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name.lower()
            try:
                is_link = child.is_symlink()
                is_directory = child.is_dir() and not is_link
            except OSError:
                continue
            if is_directory:
                resolved = child.resolve()
                if (
                    name in {".ssh", ".git"}
                    or _private_runtime_path(resolved)
                ):
                    masked_directories.append(resolved)
                elif name not in skip_directories:
                    pending.append(resolved)
            elif _sensitive_path(child):
                masked_files.append(child.resolve())
    return (
        tuple(dict.fromkeys(masked_directories)),
        tuple(dict.fromkeys(masked_files)),
    )


def _known_sensitive_files(*roots):
    """Compatibility helper returning recursively discovered sensitive files."""
    return _known_sensitive_mounts(*roots)[1]


def _bubblewrap_argv(cmd, cwd, *, trusted_execution_root=None):
    bwrap = os.environ.get("GENERICAGENT_BWRAP") or shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap is required for code_run")

    cwd_path = _resolved_path(cwd)
    args = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--cap-drop", "ALL",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
    ]
    if Path("/sys").is_dir():
        args.extend(["--tmpfs", "/sys"])

    write_roots = _configured_write_roots()
    for root in write_roots:
        if not _execution_mount_root_allowed(root):
            raise RuntimeError(f"unsafe code_run write root: {root}")
        root.mkdir(parents=True, exist_ok=True)

    execution_root = None
    if trusted_execution_root is not None:
        execution_root = _resolved_path(trusted_execution_root)
        if not _execution_root_allowed(execution_root):
            raise RuntimeError("code_run trusted execution root is not allowed")
        if not _path_within(cwd_path, execution_root):
            raise RuntimeError("code_run cwd escapes the trusted execution root")
    elif _execution_root_allowed(cwd_path):
        execution_root = cwd_path
    else:
        raise RuntimeError("code_run cwd is not an allowed execution root")
    if not cwd_path.is_dir() or not _read_allowed(cwd_path):
        raise RuntimeError("code_run cwd is not an allowed workspace directory")

    application_read_roots = _configured_code_read_roots()
    application_read_files = _configured_code_read_files()
    system_read_roots = []
    for raw in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        path = Path(raw)
        if path.is_dir():
            system_read_roots.append(path)
    for raw in (
        sys.base_prefix,
        sys.prefix,
    ):
        if not raw:
            continue
        path = _resolved_path(raw)
        if path.is_dir() and path.parent != path:
            system_read_roots.append(path)
    for root in dict.fromkeys(system_read_roots):
        args.extend(["--ro-bind", str(root), str(root)])
    for root in application_read_roots:
        args.extend(["--ro-bind", str(root), str(root)])
    for path in application_read_files:
        args.extend(["--ro-bind", str(path), str(path)])

    safe_etc_paths = (
        Path("/etc/ld.so.cache"),
        Path("/etc/localtime"),
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/ssl/certs"),
        Path("/etc/alternatives"),
    )
    for safe_path in safe_etc_paths:
        if safe_path.exists():
            args.extend(["--ro-bind", str(safe_path), str(safe_path)])

    if execution_root is not None:
        args.extend(["--ro-bind", str(execution_root), str(execution_root)])

    mounted_write_roots = tuple(dict.fromkeys(write_roots))
    for root in mounted_write_roots:
        resolved = str(root)
        args.extend(["--bind", resolved, resolved])

    sensitive_directories, sensitive_files = _known_sensitive_mounts(
        *application_read_roots,
        *mounted_write_roots,
        *((execution_root,) if execution_root is not None else ()),
    )
    for sensitive_directory in sensitive_directories:
        args.extend(["--tmpfs", str(sensitive_directory)])
    for sensitive_path in sensitive_files:
        args.extend(["--ro-bind", "/dev/null", str(sensitive_path)])
    args.extend(["--chdir", str(cwd_path), "--"])
    args.extend(cmd)
    return args


def _code_run_child_env(sandboxed=True):
    """Build a minimal execution environment without inheriting credentials.

    Sandboxed runs get a curated allowlist; host-mode runs (server admin)
    inherit the full environment so tools like git/curl/systemctl work.
    """
    if not sandboxed:
        child_env = dict(os.environ)
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        return child_env
    allowed = (
        "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    )
    child_env = {
        key: os.environ[key]
        for key in allowed
        if os.environ.get(key)
    }
    venv = os.environ.get("VIRTUAL_ENV") or os.path.join(script_dir, "venv")
    venv_bin = os.path.join(venv, "Scripts" if os.name == "nt" else "bin")
    child_env["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")
    child_env["VIRTUAL_ENV"] = venv
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return child_env


def safe_print(*args, **kwargs):
    try: print(*args, **kwargs)
    except (BrokenPipeError, OSError): pass


_SHELL_COMMAND_START = re.compile(
    r"^(?:cd|ls|find|grep|echo|printf|cat|pwd|mount|df|du|ps|git|sed|awk|"
    r"head|tail|test|mkdir|cp|mv|rm|chmod|chown|curl|wget|bash|sh)\b",
    re.IGNORECASE,
)


def _infer_code_type(code, explicit_type=None):
    """Infer an omitted code_run type while honoring every explicit choice."""
    if explicit_type is not None and str(explicit_type).strip():
        normalized = str(explicit_type).strip().lower()
        return "bash" if normalized in ("sh", "shell") else normalized
    text = str(code or "").lstrip()
    first_line = text.splitlines()[0].strip() if text else ""
    if first_line.startswith("#!") and any(
        shell in first_line.lower() for shell in ("/sh", "/bash", "/zsh")
    ):
        return "bash"
    if _SHELL_COMMAND_START.match(first_line) or "&&" in first_line:
        return "bash"
    return "python"

def code_run(code, code_type="python", timeout=60, cwd=None, code_cwd=None, stop_signal=None, maxlen=10000, myprint=safe_print, trusted_execution_root=None, sandboxed=True):
    """代码执行器
    python: 运行复杂的 .py 脚本（文件模式）
    powershell/bash: 运行单行指令（命令模式）
    优先使用python，仅在必要系统操作时使用powershell"""
    preview = (code[:60].replace('\n', ' ') + '...') if len(code) > 60 else code.strip()
    yield f"[Action] Running {code_type} in {os.path.basename(cwd)}: {preview}\n"
    cwd = cwd or os.path.join(script_dir, 'temp'); tmp_path = None
    if code_type in ["python", "py"]:
        tmp_file = tempfile.NamedTemporaryFile(suffix=".ai.py", delete=False, mode='w', encoding='utf-8', dir=code_cwd)
        cr_header = os.path.join(script_dir, 'assets', 'code_run_header.py')
        if os.path.exists(cr_header):
            with open(cr_header, encoding='utf-8') as hf:
                tmp_file.write(hf.read())
        tmp_file.write(code)
        tmp_path = tmp_file.name
        tmp_file.close()
        cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]   
    elif code_type in ["powershell", "bash", "sh", "shell", "ps1", "pwsh"]:
        if os.name == 'nt':
            _ps = "pwsh" if shutil.which("pwsh") else "powershell"
            utf8_prefix = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            cmd = [_ps, "-NoProfile", "-NonInteractive", "-Command", utf8_prefix + code]
        else: cmd = ["bash", "-o", "pipefail", "-c", code]
    else:
        return {"status": "error", "msg": f"不支持的类型: {code_type}"}
    myprint("code run output:")
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0 # SW_HIDE
    full_stdout = []
    child_env = _code_run_child_env(sandboxed=sandboxed)
    if os.name != "nt" and sandboxed:
        cmd = _bubblewrap_argv(
            cmd, cwd, trusted_execution_root=trusted_execution_root,
        )

    def stream_reader(proc, logs):
        try:
            for line_bytes in iter(proc.stdout.readline, b''):
                try: line = line_bytes.decode('utf-8')
                except UnicodeDecodeError: line = line_bytes.decode('gbk', errors='ignore')
                logs.append(line)
                myprint(line, end="")
        except Exception as e:
            myprint(f"\n[WARN] stream_reader error: {e}")

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, cwd=cwd, startupinfo=startupinfo,
            creationflags=0x08000000 if os.name == 'nt' else 0,
            env=child_env,
        )
        start_t = time.time()
        t = threading.Thread(target=stream_reader, args=(process, full_stdout), daemon=True)
        t.start()

        while t.is_alive():
            istimeout = time.time() - start_t > timeout
            if istimeout or stop_signal:
                process.kill()
                myprint("[Debug] Process killed due to timeout or stop signal.")
                if istimeout: full_stdout.append("\n[Timeout Error] 超时强制终止")
                else: full_stdout.append("\n[Stopped] 用户强制终止")
                break
            time.sleep(0.1)

        t.join(timeout=1)
        exit_code = process.poll()

        stdout_str = "".join(full_stdout)
        status = "success" if exit_code == 0 else "error"
        status_icon = "✅" if exit_code == 0 else "❌"
        if exit_code is None: status_icon = "⏳" 
        output_snippet = smart_format(stdout_str, max_str_len=600, omit_str='\n\n[omitted long output]\n\n')
        output_snippet = re.sub(r'`{4,}', lambda m: m.group(0)[:3] + '\u200b' + m.group(0)[3:], output_snippet)
        yield f"[Status] {status_icon} Exit Code: {exit_code}\n[Stdout]\n{output_snippet}\n"
        if process.stdout: threading.Thread(target=process.stdout.close, daemon=True).start()
        return {
            "status": status,
            "stdout": smart_format(stdout_str, max_str_len=maxlen, omit_str='\n\n[omitted long output]\n\n'),
            "exit_code": exit_code
        }
    except Exception as e:
        if 'process' in locals(): process.kill()
        return {"status": "error", "msg": str(e)}
    finally:
        if code_type == "python" and tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)


def ask_user(question, candidates=None):
    """question: 向用户提出的问题。candidates: 可选的候选项列表"""
    return {"status": "INTERRUPT", "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []}}

import simphtml
driver = None
_driver_lock = threading.Lock()
def first_init_driver():
    global driver
    from TMWebDriver import TMWebDriver
    driver = TMWebDriver()
    for i in range(20):
        time.sleep(1)
        sess = driver.get_all_sessions()
        if len(sess) > 0: break
    if len(sess) == 0: return 
    if len(sess) == 1: 
        #driver.newtab()
        time.sleep(3)

def web_scan(tabs_only=False, switch_tab_id=None, text_only=False, maxlen=35000):
    """获取当前页面的简化HTML内容和标签页列表。注意：简化过程会过滤边栏、浮动元素等非主体内容。
    tabs_only: 仅返回标签页列表，不获取HTML内容（节省token）。
    switch_tab_id: 可选参数，如果提供，则在扫描前切换到该标签页。
    应当多用execute_js，少全量观察html"""
    global driver
    try:
        if driver is None: first_init_driver()
        if len(driver.get_all_sessions()) == 0:
            return {"status": "error", "msg": "没有可用的浏览器标签页，查L3记忆分析原因。"}
        tabs = []
        for sess in driver.get_all_sessions(): 
            sess.pop('connected_at', None)
            sess.pop('type', None)
            sess['url'] = sess.get('url', '')[:50] + ("..." if len(sess.get('url', '')) > 50 else "")
            tabs.append(sess)
        with _driver_lock:
            if switch_tab_id: driver.default_session_id = switch_tab_id
        result = {
            "status": "success",
            "metadata": {
                "tabs_count": len(tabs), "tabs": tabs,
                "active_tab": driver.default_session_id
            }
        }
        if not tabs_only: 
            importlib.reload(simphtml); result["content"] = simphtml.get_html(driver, cutlist=True, maxchars=maxlen, text_only=text_only)
            if text_only: result['content'] = smart_format(result['content'], max_str_len=maxlen//3, omit_str='\n\n[omitted long content]\n\n')
        return result
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}
    
def format_error(e):
    exc_type, exc_value, exc_traceback = sys.exc_info()
    tb = traceback.extract_tb(exc_traceback)
    if tb:
        f = tb[-1]
        fname = os.path.basename(f.filename)
        return f"{exc_type.__name__}: {str(e)} @ {fname}:{f.lineno}, {f.name} -> `{f.line}`"
    return f"{exc_type.__name__}: {str(e)}"

def log_memory_access(path):
    if 'memory' not in path: return
    stats_file = os.path.join(script_dir, 'memory/file_access_stats.json')
    try:
        with open(stats_file, 'r', encoding='utf-8') as f:
            stats = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        stats = {}
    except Exception as e:
        print(f"[WARN] log_memory_access read failed: {e}")
        stats = {}
    fname = os.path.basename(path)
    stats[fname] = {'count': stats.get(fname, {}).get('count', 0) + 1, 'last': datetime.now().strftime('%Y-%m-%d')}
    try:
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] log_memory_access write failed: {e}")

def web_execute_js(script, switch_tab_id=None, no_monitor=False):
    """执行 JS 脚本来控制浏览器，并捕获结果和页面变化"""
    global driver
    try:
        if driver is None: first_init_driver()
        if len(driver.get_all_sessions()) == 0: return {"status": "error", "msg": "没有可用的浏览器标签页，查L3记忆分析原因。"}
        with _driver_lock:
            if switch_tab_id: driver.default_session_id = switch_tab_id
        result = simphtml.execute_js_rich(script, driver, no_monitor=no_monitor)
        return result
    except Exception as e: return {"status": "error", "msg": format_error(e)}

def _tavily_rows(query, max_results, timeout=15):
    """Tavily API search. Returns rows, or None when not configured/failed."""
    try:
        import mykey
        key = getattr(mykey, "tavily_api_key", "") or ""
    except Exception:
        key = ""
    if not key:
        return None
    payload = {
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    }
    proxy = os.environ.get("GENERICAGENT_PROXY", "") or None
    try:
        import requests
        resp = requests.post(
            "https://api.tavily.com/search",
            json=payload,
            timeout=timeout,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
        data = resp.json()
    except Exception:
        return None
    if resp.status_code != 200 or not isinstance(data, dict):
        return None
    rows = [
        {
            "title": (r.get("title") or "")[:200],
            "url": (r.get("url") or ""),
            "snippet": (r.get("content") or "")[:400],
        }
        for r in data.get("results", [])
        if r.get("url")
    ]
    return rows or None


def web_search(query, max_results=8):
    """Search the web using multiple backends with fallback. Returns {status, results, backend}."""
    q = urllib.parse.quote(query.strip())
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    last_error = None
    backends_tried = []
    collected = []
    deadline = time.monotonic() + 45

    def accept_rows(backend_name, rows):
        backends_tried.append(backend_name)
        for row in rows or []:
            collected.append(dict(row, backend=backend_name))
        return _select_relevant_results(query, collected, max_results)

    # Tavily API first (high quality, configured key only; falls through silently)
    tavily_rows = _tavily_rows(query, max_results)
    if tavily_rows:
        results = accept_rows("tavily", tavily_rows)
        if results:
            return {
                "status": "success", "results": results,
                "backend": "tavily", "backends_tried": backends_tried,
                "query": query,
            }

    # Try curl_cffi first (Chrome TLS fingerprint — bypasses most bot detection)
    try:
        from curl_cffi import requests as creq
        for backend_name, url_tpl, parse_func in _SEARCH_BACKENDS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                url = url_tpl.format(q)
                resp = creq.get(
                    url,
                    headers=headers,
                    impersonate="chrome120",
                    timeout=max(1, min(10, remaining)),
                )
                if _is_blocked(resp.text):
                    continue
                results = accept_rows(
                    backend_name,
                    parse_func(resp.text, query, max_results * 2),
                )
                if results:
                    return {
                        "status": "success", "results": results,
                        "backend": backend_name, "backends_tried": backends_tried,
                        "query": query,
                    }
            except Exception as e:
                last_error = str(e)
                continue
    except ImportError:
        pass  # curl_cffi not installed

    # Fallback: urllib (works through proxy, but search engines may block)
    for backend_name, url_tpl, parse_func in _SEARCH_BACKENDS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            url = url_tpl.format(q)
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=max(1, min(10, remaining))) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
            if _is_blocked(raw):
                continue
            results = accept_rows(
                backend_name,
                parse_func(raw, query, max_results * 2),
            )
            if results:
                return {
                    "status": "success", "results": results,
                    "backend": backend_name, "backends_tried": backends_tried,
                    "query": query,
                }
        except Exception as e:
            last_error = str(e)
            continue

    # All backends failed
    return {
        "status": "error",
        "msg": "Search backends returned no query-relevant results. "
               f"Last error: {last_error or 'blocked, empty, or irrelevant'}.",
        "query": query,
        "backends_tried": backends_tried,
    }


def _is_blocked(html):
    """Detect if a search engine returned a captcha/challenge page."""
    return any(kw in html.lower()[:2000] for kw in
               ['captcha', 'anomaly.js', 'g-recaptcha', 'are you a human',
                'challenge-form', 'challenge-platform'])


def _parse_ddg_lite(html, query, max_results):
    """Parse DuckDuckGo Lite HTML results."""
    results = []
    # DDG Lite: <a rel="nofollow" href="URL">TITLE</a> ... <td class="result-snippet">SNIPPET</td>
    link_pattern = re.compile(
        r'<a\s[^>]*?href="(https?://[^"]+)"[^>]*?>(.+?)</a>', re.DOTALL)
    snippet_pattern = re.compile(
        r'<td\s+class="result-snippet"[^>]*?>(.*?)</td>', re.DOTALL)
    links = list(link_pattern.finditer(html))
    snippets = list(snippet_pattern.finditer(html))
    for li in links[:max_results]:
        url = li.group(1)
        title = re.sub(r'<[^>]+>', '', li.group(2)).strip()
        title = _html.unescape(title)
        snippet = ""
        for si in snippets:
            if si.start() > li.end() and si.start() < li.end() + 2000:
                snippet = re.sub(r'<[^>]+>', '', si.group(1)).strip()
                snippet = _html.unescape(snippet)
                break
        if title and url and 'duckduckgo.com' not in url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _parse_ddg_html(html, query, max_results):
    """Parse DuckDuckGo HTML (non-lite) results."""
    results = []
    # DDG HTML: <div class="result__body"> with result__title, result__snippet, result__url
    body_pattern = re.compile(
        r'<a\s+class="result__a"[^>]*?href="(https?://[^"]+)"[^>]*?>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(
        r'<a\s+class="result__snippet"[^>]*?>(.*?)</a>', re.DOTALL)
    links = list(body_pattern.finditer(html))
    snippets = list(snippet_pattern.finditer(html))
    for li in links[:max_results]:
        url = li.group(1)
        title = re.sub(r'<[^>]+>', '', li.group(2)).strip()
        title = _html.unescape(title)
        snippet = ""
        for si in snippets:
            if si.start() > li.end() and si.start() < li.end() + 3000:
                snippet = re.sub(r'<[^>]+>', '', si.group(1)).strip()
                snippet = _html.unescape(snippet)
                break
        if title and url and 'duckduckgo.com' not in url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _parse_bing(html, query, max_results):
    """Parse Bing search results."""
    results = []
    # Bing: <li class="b_algo"> containing <h2><a href="URL">TITLE</a></h2> and <p>SNIPPET</p>
    algo_pattern = re.compile(
        r'<li\s+class="b_algo"[^>]*?>(.*?)</li>', re.DOTALL)
    for block in algo_pattern.finditer(html):
        if len(results) >= max_results:
            break
        block_html = block.group(1)
        link_m = re.search(
            r'<h2[^>]*>\s*<a[^>]*?href="(https?://[^"]+)"[^>]*?>(.*?)</a>\s*</h2>',
            block_html,
            re.DOTALL,
        )
        if not link_m:
            continue
        url = link_m.group(1)
        title = re.sub(r'<[^>]+>', '', link_m.group(2)).strip()
        title = _html.unescape(title)
        # Bing snippet is in <p> or <div class="b_caption">
        caption_m = re.search(
            r'<div\s+class="b_caption"[^>]*>(.*?)</div>',
            block_html,
            re.DOTALL,
        )
        caption = caption_m.group(1) if caption_m else block_html
        snip_m = re.search(r'<p[^>]*>(.{10,500}?)</p>', caption, re.DOTALL)
        snippet = re.sub(r'<[^>]+>', '', snip_m.group(1)).strip() if snip_m else ""
        snippet = _html.unescape(snippet)
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


_SEARCH_STOPWORDS = {
    "a", "an", "and", "the", "for", "of", "to", "on", "in",
    "new", "game", "latest", "news", "announcement", "official",
    "documentation", "消息", "最新", "有关", "总结", "新作",
}
_ACTION_MARKERS = (
    "new", "news", "latest", "announce",
    "project", "release", "upcoming", "新作", "新游戏", "新项目", "公布",
    "发布", "发售", "発売", "最新", "消息",
)


def _query_terms(query):
    lowered = (query or "").lower()
    latin = re.findall(r"[a-z0-9]{2,}", lowered)
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    return [
        term for term in latin + chinese
        if term not in _SEARCH_STOPWORDS and not re.fullmatch(r"20\d{2}", term)
    ]


def _relevance_score(query, row):
    title = str(row.get("title", "")).lower()
    haystack = " ".join([
        title,
        str(row.get("snippet", "")),
        str(row.get("url", "")),
    ]).lower()
    query_lower = (query or "").lower()
    action_intent = any(marker in query_lower for marker in (
        "new", "latest", "announcement", "新作", "最新",
    ))
    if action_intent and not any(marker in haystack for marker in _ACTION_MARKERS):
        return 0
    terms = _query_terms(query)
    return sum(3 if term in title else 1 for term in terms if term in haystack)


def _select_relevant_results(query, rows, max_results):
    deduped = {}
    for row in rows:
        url = str(row.get("url", "")).split("#", 1)[0]
        score = _relevance_score(query, row)
        if not url or score <= 0:
            continue
        candidate = dict(row, url=url, relevance=score)
        if url not in deduped or score > deduped[url]["relevance"]:
            deduped[url] = candidate
    return sorted(
        deduped.values(),
        key=lambda row: (-row["relevance"], row["url"]),
    )[:max_results]


def _parse_google(html, query, max_results):
    """Parse Google search results."""
    results = []
    # Google wraps each result in a div with data-hveid or jsname
    # Title: <a href="URL"><h3>TITLE</h3></a>  or <div class="BNeawe">TITLE</div>
    # Snippet: <div class="BNeawe"> or <span class="aCOpRe">

    # Method 1: Find <h3> inside <a> (most common pattern)
    for m in re.finditer(r'<a\s+href="(https?://[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>', html, re.DOTALL):
        url = _html.unescape(m.group(1))
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        title = _html.unescape(title)
        # Find snippet after this result
        snippet = ""
        after = html[m.end():m.end()+2000]
        snip_m = re.search(r'(?:<div[^>]*class="[^"]*(?:BNeawe|VwiC3b|st)[^"]*"[^>]*>|'
                          r'<span[^>]*class="[^"]*aCOpRe[^"]*"[^>]*>)(.*?)'
                          r'(?:</div>|</span>)', after, re.DOTALL)
        if snip_m:
            snippet = re.sub(r'<[^>]+>', '', snip_m.group(1)).strip()
            snippet = _html.unescape(snippet)
        if title and url and 'google.com' not in url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


# Ordered search backends: (name, url_template, parser)
_SEARCH_BACKENDS = [
    ("ddg_lite", "https://lite.duckduckgo.com/lite/?q={}", _parse_ddg_lite),
    ("ddg_html", "https://html.duckduckgo.com/html/?q={}", _parse_ddg_html),
    ("bing", "https://www.bing.com/search?q={}&setlang=en", _parse_bing),
    ("google", "https://www.google.com/search?q={}&hl=en", _parse_google),
]


def web_fetch(url, max_chars=6000):
    """Fetch a URL and extract readable text. Strips HTML tags/scripts/styles."""
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            # Check content type — skip non-HTML
            ct = resp.headers.get('Content-Type', '')
            raw = resp.read()

        # Try to decode
        for enc in ['utf-8', 'gbk', 'latin-1']:
            try:
                html = raw.decode(enc, errors='strict')
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            html = raw.decode('utf-8', errors='replace')

        # Strip scripts, styles, and HTML tags
        html = re.sub(r'<(script|style|noscript|iframe|svg)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<(script|style|noscript|iframe|svg)\s[^>]*?/>', '', html, flags=re.IGNORECASE)
        # Remove HTML comments
        html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
        # Convert block-level tags to newlines
        html = re.sub(r'</?(?:div|p|h[1-6]|li|tr|br|article|section|header|footer|main|aside|nav|table|ul|ol|dl|blockquote|pre|hr)[^>]*>', '\n', html, flags=re.IGNORECASE)
        # Remove remaining tags
        text = re.sub(r'<[^>]+>', '', html)
        # Decode HTML entities
        text = _html.unescape(text)
        # Collapse whitespace
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars, original length: {len(text)}]"

        result_url = resp.geturl()  # final URL after redirects
        return {"status": "success", "content": text, "url": result_url, "length": len(text)}

    except urllib.error.HTTPError as e:
        return {"status": "error", "msg": f"HTTP {e.code}: {e.reason}", "url": url}
    except urllib.error.URLError as e:
        return {"status": "error", "msg": f"Network error: {e.reason}", "url": url}
    except Exception as e:
        return {"status": "error", "msg": format_error(e), "url": url}


def expand_file_refs(text, base_dir=None):
    """展开文本中的 {{file:路径:起始行:结束行}} 引用为实际文件内容。
    可与普通文本混排。展开失败抛 ValueError。
    base_dir: 相对路径的基准目录，默认为进程 cwd"""
    pattern = r'\{\{file:(.+?):(\d+):(\d+)\}\}'
    def replacer(match):
        path, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        path = os.path.abspath(os.path.join(base_dir or '.', path))
        if not _read_allowed(path):
            raise ValueError(_read_denied_result(path)["msg"])
        if not os.path.isfile(path): raise ValueError(f"引用文件不存在: {path}")
        with open(path, 'r', encoding='utf-8') as f: lines = f.readlines()
        if start < 1 or end > len(lines) or start > end: raise ValueError(f"行号越界: {path} 共{len(lines)}行, 请求{start}-{end}")
        return ''.join(lines[start-1:end])
    return re.sub(pattern, replacer, text)
    
def file_patch(path: str, old_content: str, new_content: str, automatic=False):
    """在文件中寻找唯一的 old_content 块并替换为 new_content"""
    path = str(Path(path).resolve())
    try:
        if not (_write_allowed(path) or _memory_patch_allowed(path)):
            return _write_denied_result(path)
        if not os.path.exists(path): return {"status": "error", "msg": "文件不存在"}
        with open(path, 'r', encoding='utf-8') as f: full_text = f.read()
        if not old_content: return {"status": "error", "msg": "old_content 为空，请确认 arguments"}
        count = full_text.count(old_content)
        if count == 0: return {"status": "error", "msg": "未找到匹配的旧文本块，建议：先用 file_read 确认当前内容，再分小段进行 patch。若多次失败则询问用户，严禁自行使用 overwrite 或代码替换。"}
        if count > 1: return {"status": "error", "msg": f"找到 {count} 处匹配，无法确定唯一位置。请提供更长、更具体的旧文本块以确保唯一性。建议：包含上下文行来增强特征，或分小段逐个修改。"}
        rejection = _memory_rejection_reason(new_content) if _memory_content_allowed(path) else ""
        if rejection:
            return {"status": "error", "msg": f"Memory update rejected: {rejection}"}
        updated_text = full_text.replace(old_content, new_content)
        if _memory_content_allowed(path):
            validate_memory_content(
                path, updated_text, previous_content=full_text,
                automatic=bool(automatic),
            )
            _atomic_memory_write(path, updated_text)
            _audit_memory_write("patch", path)
        else:
            with open(path, 'w', encoding='utf-8') as f: f.write(updated_text)
        return {"status": "success", "msg": "文件局部修改成功"}
    except Exception as e: return {"status": "error", "msg": str(e)}

_read_dirs = set()
def _scan_files(base, depth=2):
    try:
        for e in os.scandir(base):
            if e.is_file(): yield (e.name, e.path)
            elif depth > 0 and e.is_dir(follow_symlinks=False): yield from _scan_files(e.path, depth - 1)
    except (PermissionError, OSError): pass
def file_read(path, start=1, keyword=None, count=200, show_linenos=True):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            stream = ((i, l.rstrip('\r\n')) for i, l in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < start, stream)
            if keyword:
                before = collections.deque(maxlen=count//3)
                for i, l in stream:
                    if keyword.lower() in l.lower():
                        res = list(before) + [(i, l)] + list(itertools.islice(stream, count - len(before) - 1))
                        break
                    before.append((i, l))
                else: return f"Keyword '{keyword}' not found after line {start}. Falling back to content from line {start}:\n\n" \
                               + file_read(path, start, None, count, show_linenos)
            else: res = list(itertools.islice(stream, count))
            realcnt = len(res); L_MAX = min(max(100, 256000//max(realcnt,1)), 8000); TAG = " ... [TRUNCATED]"
            remaining = sum(1 for _ in itertools.islice(stream, 5000))
            total_lines = (res[0][0] - 1 if res else start - 1) + realcnt + remaining
            tl_str = f"{total_lines}+" if remaining >= 5000 else str(total_lines)
            partial = total_lines > realcnt
            total_tag = f"[FILE] {tl_str} lines" + (f" | PARTIAL showing {realcnt}; assess need for more" if partial else "") + "\n"
            res = [(i, l if len(l) <= L_MAX else l[:L_MAX] + TAG) for i, l in res]
            result = "\n".join(f"{i}|{l}" if show_linenos else l for i, l in res)
            if show_linenos: result = total_tag + result
            elif partial: result += f"\n\n[FILE PARTIAL: showing {realcnt}/{tl_str} lines; assess need for more]"
            _read_dirs.add(os.path.dirname(os.path.abspath(path)))
            return result
    except FileNotFoundError:
        msg = f"Error: File not found: {path}"
        try:
            tgt = os.path.basename(path); scan = os.path.dirname(os.path.dirname(os.path.abspath(path)))
            roots = [scan] + [d for d in _read_dirs if not d.startswith(scan)]
            cands = list(itertools.islice((c for base in roots for c in _scan_files(base)), 2000))
            top = sorted([(difflib.SequenceMatcher(None, tgt.lower(), c[0].lower()).ratio(), c) for c in cands[:2000]], key=lambda x: -x[0])[:5]
            top = [(s, c) for s, c in top if s > 0.3]
            if top: msg += "\n\nDid you mean:\n" + "\n".join(f"  {c[1]}  ({s:.0%})" for s, c in top)
        except Exception: pass
        return msg
    except Exception as e: return f"Error: {str(e)}"

def smart_format(data, max_str_len=100, omit_str=' ... '):
    if not isinstance(data, str): data = str(data)
    if len(data) < max_str_len + len(omit_str)*2: return data
    return f"{data[:max_str_len//2]}{omit_str}{data[-max_str_len//2:]}"

def consume_file(dr, file):
    if dr and os.path.exists(os.path.join(dr, file)): 
        with open(os.path.join(dr, file), encoding='utf-8', errors='replace') as f: content = f.read()
        os.remove(os.path.join(dr, file))
        return content

class GenericAgentHandler(BaseHandler):
    '''Generic Agent 工具库，包含多种工具的实现。工具函数自动加上了 do_ 前缀。实际工具名没有前缀。'''
    def __init__(self, parent, last_history=None, cwd='./temp', allow_inline_eval=True,
                 memory_only=False):
        self.parent = parent
        self.working = {}
        self.cwd = cwd;  self.current_turn = 0
        self.history_info = last_history if last_history else []
        self.code_stop_signal = []
        self._done_hooks = []
        self.allow_inline_eval = bool(allow_inline_eval)
        self.memory_only = bool(memory_only)
        self.print = safe_print

    def _get_tool_maxlen(self, length, args, growth_rate=1.0):
        get_multiplier = getattr(self.parent, 'get_ctx_multiplier', lambda: 1.0)
        multiplier = 1 + (get_multiplier() - 1) * growth_rate
        return int(length * multiplier / args.get('_tool_num', 1))

    def _get_abs_path(self, path):
        if not path: return ""
        return os.path.abspath(os.path.join(self.cwd, path))   

    def _extract_code_block(self, response, code_type):
        code_type = {'python':'python|py', 'powershell':'powershell|ps1|pwsh', 'bash':'bash|sh|shell'}.get(code_type, re.escape(code_type))
        matches = re.findall(rf"```(?:{code_type})\n(.*?)\n```", response.content, re.DOTALL)
        return matches[-1].strip() if matches else None

    def do_code_run(self, args, response):
        '''执行代码片段，有长度限制，不允许代码中放大量数据，如有需要应当通过文件读取进行。'''
        code = args.get("code") or args.get("script")
        code_type = _infer_code_type(code, args.get("type"))
        if not code:
            code = self._extract_code_block(response, code_type)
            if not code: return StepOutcome("[Error] Code missing. Must use reply code block or 'script' arg.", next_prompt="\n")
        if args.get("inline_eval") and not self.allow_inline_eval:
            denied = {
                "status": "error",
                "msg": "inline_eval is disabled for remote chat requests",
            }
            yield f"[Status] ❌ {denied['msg']}\n"
            return StepOutcome(denied, next_prompt="\n")
        # Host mode: unsandboxed server administration. Bash only so the
        # dangerous-command guardrail below always applies; python stays in
        # the sandbox (its os/system calls would bypass the bash checks).
        sandboxed = _sandbox_enabled()
        mode = str(args.get("mode") or "").lower()
        if mode == "host":
            if code_type not in ("bash", "sh", "shell"):
                denied = {
                    "status": "error",
                    "msg": "host 模式仅支持 bash/sh；python 请用默认沙箱模式",
                }
                yield f"[Status] ❌ {denied['msg']}\n"
                return StepOutcome(denied, next_prompt="\n")
            sandboxed = False
            chat_id = str((self.parent.active_task or {}).get("chat_id") or "?")
            _audit_admin_op(code[:800], f"chat:{chat_id}")
        elif mode not in ("", "sandbox"):
            denied = {
                "status": "error",
                "msg": f"未知 mode: {mode}（可选 sandbox/host）",
            }
            yield f"[Status] ❌ {denied['msg']}\n"
            return StepOutcome(denied, next_prompt="\n")
        # Guardrails: check dangerous patterns (both bash and python with os/system/subprocess)
        if code_type in ('bash', 'sh'):
            dangerous, reason = _check_dangerous_command(code)
            if dangerous:
                msg = f"⛔ 危险命令拦截: {reason}\n如需执行请确认。"
                yield msg + "\n"
                return StepOutcome({"error": msg}, next_prompt=msg)
        try: timeout = int(args.get("timeout", 120))
        except (ValueError, TypeError): timeout = 120
        try:
            trusted_root, cwd_path = _resolve_code_run_cwd(
                self.cwd, args.get("cwd", "./"),
            )
        except (OSError, PermissionError, ValueError) as error:
            denied = {"status": "error", "msg": str(error)}
            yield f"[Status] ❌ {denied['msg']}\n"
            return StepOutcome(denied, next_prompt="\n")
        cwd = str(cwd_path)
        code_cwd = str(trusted_root)
        maxlen = max(self._get_tool_maxlen(10000, args), 3000)  # floor at 3000
        if code_type == 'python' and args.get("inline_eval"):
            ns = {'handler':self, 'parent':self.parent, 'history':json.dumps(self.parent.llmclient.backend.history)}
            old_cwd = os.getcwd()
            try:
                os.chdir(cwd)
                try:
                    try: result = repr(eval(code, ns))
                    except SyntaxError: exec(code, ns); result = ns.get('_r', 'OK')
                except Exception as e: result = f'Error: {e}'
            finally: os.chdir(old_cwd)
        else: result = yield from code_run(
            code, code_type, timeout, cwd, code_cwd=code_cwd,
            stop_signal=self.code_stop_signal, maxlen=maxlen,
            myprint=self.print, trusted_execution_root=str(trusted_root),
            sandboxed=sandboxed,
        )
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        recovery_hint = _code_run_memory_recovery_hint(code, result)
        if recovery_hint:
            result = dict(result)
            result["recovery_hint"] = recovery_hint
            next_prompt += f"\n\n[SYSTEM RECOVERY] {recovery_hint}"
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_ask_user(self, args, response):
        question = args.get("question", "请提供输入：")
        candidates = args.get("candidates", [])
        result = ask_user(question, candidates)
        yield f"Waiting for your answer ...\n"
        return StepOutcome(result, next_prompt="", should_exit=True)

    def do_session_search(self, args, response):
        """Search durable history within the current transport-neutral conversation."""
        identity = dict((getattr(self.parent, "active_task", None) or {}).get("conversation_identity") or {})
        if not identity:
            return StepOutcome({"status": "error", "msg": "conversation identity unavailable"}, next_prompt="\n")
        try:
            from session_store import ConversationIdentity
            cid = ConversationIdentity(**{
                key: identity.get(key, "")
                for key in ("platform", "account", "conversation", "actor")
            })
            rows = self.parent.session_store.search(
                cid, args.get("query", ""), args.get("limit", 5)
            )
            result = {"status": "success", "results": rows}
        except Exception as error:
            result = {"status": "error", "msg": str(error)}
        yield f"[Status] session search returned {len(result.get('results', []))} result(s)\n"
        return StepOutcome(result, next_prompt="\n")

    def do_skill_propose(self, args, response):
        """Stage a Markdown Skill proposal; never activates it."""
        identity = dict((getattr(self.parent, "active_task", None) or {}).get("conversation_identity") or {})
        if not identity:
            return StepOutcome({"status": "error", "msg": "conversation identity unavailable"}, next_prompt="\n")
        try:
            from session_store import ConversationIdentity
            cid = ConversationIdentity(**{
                key: identity.get(key, "")
                for key in ("platform", "account", "conversation", "actor")
            })
            proposal = self.parent.skill_manager.propose(
                args.get("name", ""), args.get("content", ""),
                args.get("reason", ""), cid,
            )
            result = {
                "status": "pending_approval", "proposal_id": proposal["id"],
                "name": proposal["slug"], "sha256": proposal["sha256"],
                "message": f"Skill proposal staged. Approve with /skill approve {proposal['id']}",
            }
        except Exception as error:
            result = {"status": "error", "msg": str(error)}
        yield f"[Status] {result.get('status')}\n"
        return StepOutcome(result, next_prompt="\n")

    def do_source_change_propose(self, args, response):
        """Stage an exact source/configuration patch for a bound QQ approver."""
        identity = dict((getattr(self.parent, "active_task", None) or {}).get("conversation_identity") or {})
        if not identity:
            return StepOutcome(
                {"status": "error", "msg": "conversation identity unavailable"},
                next_prompt="\n",
            )
        try:
            from session_store import ConversationIdentity
            cid = ConversationIdentity(**{
                key: identity.get(key, "")
                for key in ("platform", "account", "conversation", "actor")
            })
            path = self._get_abs_path(args.get("path", ""))
            proposal = self.parent.change_approval.propose_patch(
                cid, path, args.get("old_content", ""),
                args.get("new_content", ""), args.get("reason", ""),
            )
            commands = {
                "normal": f"批准执行 {proposal['id']}",
                "high": f"确认高风险 {proposal['id']}",
                "emergency": f"查看差异 {proposal['id']}（获取紧急授权挑战码）",
            }
            result = {
                "status": "pending_approval",
                "proposal_id": proposal["id"],
                "risk": proposal["risk"],
                "path": proposal["path"],
                "before_sha256": proposal["before_sha256"],
                "after_sha256": proposal["after_sha256"],
                "expires_at": proposal["expires_at"],
                "approval_command": commands[proposal["risk"]],
                "message": "Exact change staged; no source file was modified.",
            }
        except Exception as error:
            result = {"status": "error", "msg": str(error)}
        yield f"[Status] {result.get('status')}\n"
        return StepOutcome(result, next_prompt="\n")
    
    def do_web_scan(self, args, response):
        '''获取当前页面内容和标签页列表。也可用于切换标签页。
        注意：HTML经过简化，边栏/浮动元素等可能被过滤。如需查看被过滤的内容请用execute_js。
        tabs_only=true时仅返回标签页列表，不获取HTML（省token）'''
        tabs_only = args.get("tabs_only", False)
        switch_tab_id = args.get("switch_tab_id", None)
        text_only = args.get("text_only", False)
        maxlen = self._get_tool_maxlen(35000, args, growth_rate=0.5)
        result = web_scan(tabs_only=tabs_only, switch_tab_id=switch_tab_id, text_only=text_only, maxlen=maxlen)
        content = result.pop("content", None)
        yield f'[Info] {str(result)}\n'
        if content: result = json.dumps(result, ensure_ascii=False, default=json_default) + f"\n```html\n{content}\n```"
        next_prompt = "\n"
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_web_execute_js(self, args, response):
        '''web情况下的优先使用工具，执行任何js达成对浏览器的*完全*控制。支持将结果保存到文件供后续读取分析。'''
        script = args.get("script", "") or self._extract_code_block(response, "javascript")
        if not script: return StepOutcome("[Error] Script missing. Use ```javascript block or 'script' arg.", next_prompt="\n")
        abs_path = self._get_abs_path(script.strip())
        if os.path.isfile(abs_path):
            if not _read_allowed(abs_path):
                denied = _read_denied_result(abs_path)
                yield f"[Status] ❌ {denied['msg']}\n"
                return StepOutcome(denied, next_prompt="\n")
            with open(abs_path, 'r', encoding='utf-8') as f: script = f.read()
        save_to_file = args.get("save_to_file", "")
        switch_tab_id = args.get("switch_tab_id") or args.get("tab_id")
        no_monitor = args.get("no_monitor", False)
        result = web_execute_js(script, switch_tab_id=switch_tab_id, no_monitor=no_monitor)
        if save_to_file and "js_return" in result:
            content = str(result["js_return"] or '')
            abs_path = self._get_abs_path(save_to_file)
            result["js_return"] = smart_format(content, max_str_len=170)
            if not _write_allowed(abs_path):
                denied = _write_denied_result(abs_path)
                result["js_return"] += f"\n\n[保存失败，{denied['msg']}]"
            else:
                try:
                    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                    with open(abs_path, 'w', encoding='utf-8') as f: f.write(str(content))
                    result["js_return"] += f"\n\n[已保存完整内容到 {abs_path}]"
                except Exception:
                    result['js_return'] += f"\n\n[保存失败，无法写入文件 {abs_path}]"
        show = smart_format(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), max_str_len=300)
        self.print("Web Execute JS Result:", show)
        yield f"JS 执行结果:\n{show}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        result = json.dumps(result, ensure_ascii=False, default=json_default)
        maxlen = self._get_tool_maxlen(8000, args)
        return StepOutcome(smart_format(result, max_str_len=maxlen), next_prompt=next_prompt)

    def do_web_search(self, args, response):
        '''搜索网页。用于查找未知信息、验证事实、获取最新文档。
        返回标题、URL和摘要。获取到URL后可继续用 web_fetch 读取全文。'''
        query = args.get("query", "")
        if not query:
            yield "[Error] Query is required for web_search.\n"
            return StepOutcome({"status": "error", "msg": "Missing query"}, next_prompt="\n")
        max_results = min(int(args.get("max_results", 8)), 15)
        yield f"[Action] Searching web: {query[:80]}\n"
        result = web_search(query, max_results=max_results)
        if result.get("status") == "error":
            yield f"[Status] ❌ Search failed: {result.get('msg')}\n"
            next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
            return StepOutcome(result, next_prompt=next_prompt)
        results = result.get("results", [])
        yield f"[Status] ✅ Found {len(results)} results\n"
        lines = [f"Web search results for: {query}"]
        for i, r in enumerate(results, 1):
            lines.append(f"\n{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
        formatted = "\n".join(lines)
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome({"status": "success", "results": results, "formatted": formatted}, next_prompt=next_prompt)

    def do_web_fetch(self, args, response):
        '''抓取URL并提取正文。自动去标签/脚本/样式。web_search拿到URL后的下一步。'''
        url = args.get("url", "")
        if not url:
            yield "[Error] URL is required for web_fetch.\n"
            return StepOutcome({"status": "error", "msg": "Missing url"}, next_prompt="\n")
        max_chars = min(int(args.get("max_chars", 6000)), 15000)
        save_to_file = args.get("save_to_file", "")
        yield f"[Action] Fetching: {url[:100]}\n"
        result = web_fetch(url, max_chars=max_chars)
        if result.get("status") == "error":
            yield f"[Status] ❌ Fetch failed: {result.get('msg')}\n"
            next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
            return StepOutcome(result, next_prompt=next_prompt)
        content = result.get("content", "")
        yield f"[Status] ✅ Fetched {len(content)} chars from {result.get('url', url)[:60]}\n"
        if save_to_file:
            abs_path = self._get_abs_path(save_to_file)
            if not _write_allowed(abs_path):
                denied = _write_denied_result(abs_path)
                yield f"[Status] ❌ {denied['msg']}\n"
                return StepOutcome(denied, next_prompt="\n")
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(content)
            yield f"[Status] ✅ 已保存到 {abs_path}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_file_patch(self, args, response):
        path = self._get_abs_path(args.get("path", ""))
        yield f"[Action] Patching file: {path}\n"
        if self.memory_only and not _memory_patch_allowed(path):
            result = {"status": "error", "msg": f"Memory settlement may patch only existing top-level memory text: {path}"}
            yield f"[Status] ❌ {result['msg']}\n"
            return StepOutcome(result, next_prompt="\n")
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")
        try: new_content = expand_file_refs(new_content, base_dir=self.cwd)
        except ValueError as e:
            yield f"[Status] ❌ 引用展开失败: {e}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        result = file_patch(path, old_content, new_content, automatic=self.memory_only)
        yield f"\n{str(result)}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_file_write(self, args, response):
        '''用于对整个文件的大量处理，精细修改要用file_patch。
        需要将要写入的内容放在<file_content>标签内，或者放在代码块中'''
        path = self._get_abs_path(args.get("path", ""))
        mode = args.get("mode", "overwrite")  # overwrite/append/prepend
        action_str = {"prepend": "Prepending to", "append": "Appending to"}.get(mode, "Overwriting")
        yield f"[Action] {action_str} file: {os.path.basename(path)}\n"
        normal_write = _write_allowed(path) and not self.memory_only
        memory_create = _memory_create_allowed(path)
        if not (normal_write or memory_create):
            result = _write_denied_result(path)
            yield f"[Status] ❌ {result['msg']}\n"
            return StepOutcome(result, next_prompt="\n")
        if memory_create and mode != "overwrite":
            result = {
                "status": "error",
                "msg": "Memory file_write only supports creating a new top-level .md file in overwrite mode.",
            }
            yield f"[Status] ❌ {result['msg']}\n"
            return StepOutcome(result, next_prompt="\n")

        def extract_robust_content(text):
            tags = re.findall(r"<file_content[^>]*>(.*?)</file_content>", text, re.DOTALL)
            if tags: return tags[-1].strip()
            blocks = re.findall(r"```[^\n]*\n([\s\S]*?)```", text)
            if blocks: return blocks[-1].strip()
            return None
        
        content = args.get('content') or extract_robust_content(response.content)
        if not content:
            yield f"[Status] ❌ 失败: 未在回复中找到<file_content>代码块内容\n"
            return StepOutcome({"status": "error", "msg": "No content found. Blank is not supported. Put content inside <file_content>...</file_content> tags in your reply body before call file_write."}, next_prompt="\n")
        rejection = _memory_rejection_reason(content) if memory_create else ""
        if rejection:
            result = {"status": "error", "msg": f"Memory update rejected: {rejection}"}
            yield f"[Status] ❌ {result['msg']}\n"
            return StepOutcome(result, next_prompt="\n")
        try:
            new_content = expand_file_refs(content, base_dir=self.cwd)
            rejection = _memory_rejection_reason(new_content) if memory_create else ""
            if rejection:
                raise ValueError(f"Memory update rejected: {rejection}")
            if memory_create:
                validate_memory_content(
                    path, new_content, previous_content="",
                    automatic=self.memory_only,
                )
            if mode == "prepend":
                old = ""
                try:
                    with open(path, 'r', encoding="utf-8") as f:
                        old = f.read()
                except FileNotFoundError:
                    pass
                with open(path, 'w', encoding="utf-8") as f:
                    f.write(new_content + old)
            elif memory_create:
                with open(path, 'x', encoding="utf-8") as f: f.write(new_content)
                os.chmod(path, 0o640)
                _audit_memory_write("create", path)
            else:
                with open(path, 'a' if mode == "append" else 'w', encoding="utf-8") as f: f.write(new_content)
            yield f"[Status] ✅ {mode.capitalize()} 成功 ({len(new_content)} bytes)\n"
            next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
            return StepOutcome({"status": "success", 'writed_bytes': len(new_content)}, next_prompt=next_prompt)
        except Exception as e:
            yield f"[Status] ❌ 写入异常: {str(e)}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        
    def do_file_read(self, args, response):
        '''读取文件内容。从第start行开始读取。如有keyword则返回第一个keyword(忽略大小写)周边内容'''
        path = self._get_abs_path(args.get("path", ""))
        yield f"\n[Action] Reading file: {path}\n"
        if not _read_allowed(path):
            denied = _read_denied_result(path)
            yield f"[Status] ❌ {denied['msg']}\n"
            return StepOutcome(denied, next_prompt="\n")
        start = args.get("start", 1)
        count = args.get("count", 200)
        keyword = args.get("keyword")
        show_linenos = args.get("show_linenos", True)
        result = file_read(path, start=start, keyword=keyword,
                           count=count, show_linenos=show_linenos)
        if show_linenos and not result.startswith("Error:"): result = '由于设置了show_linenos，以下返回信息为：(行号|)内容 。\n' + result 
        if ' ... [TRUNCATED]' in result: result += '\n\n（某些行被截断，如需完整内容可改用 code_run 读取）'
        maxlen = self._get_tool_maxlen(15000, args)
        result = smart_format(result, max_str_len=maxlen, omit_str='\n\n[omitted long content]\n\n')
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        log_memory_access(path)
        if 'memory' in path or 'sop' in path: 
            next_prompt += "\n[SYSTEM TIPS] 正在读取记忆或SOP文件，若决定按sop执行请提取sop中的关键点（特别是靠后的）update working memory."
        return StepOutcome(result, next_prompt=next_prompt)
    
    def _in_plan_mode(self): return self.working.get('in_plan_mode')
    def _exit_plan_mode(self): self.working.pop('in_plan_mode', None)
    def enter_plan_mode(self, plan_path): 
        self.working['in_plan_mode'] = plan_path; self.max_turns = 100
        self.print(f"[Info] Entered plan mode with plan file: {plan_path}")
        return plan_path
    def _check_plan_completion(self):
        if not os.path.isfile(p:=self._in_plan_mode() or ''): return None
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                return len(re.findall(r'\[ \]', f.read()))
        except (OSError, UnicodeDecodeError): return None
    
    def do_update_working_checkpoint(self, args, response):
        '''为整个任务设定后续需要临时记忆的重点。'''
        key_info = args.get("key_info", "")
        related_sop = args.get("related_sop", "")
        if "key_info" in args: self.working['key_info'] = key_info
        if "related_sop" in args: self.working['related_sop'] = related_sop
        self.working['passed_sessions'] = 0
        yield f"[Info] Updated key_info and related_sop.\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        #next_prompt += '\n[SYSTEM TIPS] 此函数一般在任务开始或中间时调用，如果任务已成功完成应该是start_long_term_update用于结算长期记忆。\n'
        return StepOutcome({"result": "working key_info updated"}, next_prompt=next_prompt)

    def _retry_or_exit(self, prompt):
        self._empty_ct = getattr(self, '_empty_ct', 0) + 1
        if self._empty_ct >= 3: return StepOutcome({}, should_exit=True)
        return StepOutcome({}, next_prompt=prompt)

    def do_no_tool(self, args, response):
        '''这是一个特殊工具，由引擎自主调用，不要包含在TOOLS_SCHEMA里。
        当模型在一轮中未显式调用任何工具时，由引擎自动触发。
        二次确认仅在回复几乎只包含<thinking>/<summary>和一段大代码块时触发。'''
        content = getattr(response, 'content', '') or ""
        thinking = getattr(response, 'thinking', '') or ""
        if not response or (not content.strip() and not thinking.strip()):
            yield "[Warn] LLM returned an empty response. Retrying...\n"
            return self._retry_or_exit("[System] Blank response, regenerate and tooluse")
        if '[!!! 流异常中断' in content[-100:] or '!!!Error:' in content[-100:]:
            return self._retry_or_exit("[System] Incomplete response. Regenerate and tooluse.")
        if 'max_tokens !!!]' in content[-100:]:
            return self._retry_or_exit("[System] max_tokens limit reached. Use multi small steps to do it.")
        
        if self._in_plan_mode() and any(kw in content for kw in ['任务完成', '全部完成', '已完成所有', '🏁']):
            if 'VERDICT' not in content and '[VERIFY]' not in content and '验证subagent' not in content:
                yield "[Warn] Plan模式完成声明拦截。\n"
                return StepOutcome({}, next_prompt="⛔ [验证拦截] 检测到你在plan模式下声称完成，但未执行[VERIFY]验证步骤。请先按plan_sop §四启动验证subagent，获得VERDICT后才能声称完成。")
            
        # 2. 检测"包含较大代码块但未调用工具"的情况
        # 关键特征：恰好1个大代码块 + 代码块直接结尾（后面只有空白）
        code_block_pattern = r"```[a-zA-Z0-9_]*\n[\s\S]{50,}?```"
        blocks = re.findall(code_block_pattern, content)
        if len(blocks) == 1:
            m = re.search(code_block_pattern, content)
            after_block = content[m.end():]
            if not after_block.strip():
                residual = content.replace(m.group(0), "")
                residual = re.sub(r"<thinking>[\s\S]*?</thinking>", "", residual, flags=re.IGNORECASE)
                residual = re.sub(r"<summary>[\s\S]*?</summary>", "", residual, flags=re.IGNORECASE)
                clean_residual = re.sub(r"\s+", "", residual)
                if len(clean_residual) <= 30:
                    yield "[Info] Detected large code block without tool call and no extra natural language. Requesting clarification.\n"
                    next_prompt = (
                        "[System] 检测到你在上一轮回复中主要内容是较大代码块，且本轮未调用任何工具。\n"
                        "如果这些代码需要执行、写入文件或进一步分析，请重新组织回复并显式调用相应工具"
                        "（例如：code_run、file_write、file_patch 等）；\n"
                        "如果只是向用户展示或讲解代码片段，请在回复中补充自然语言说明，"
                        "并明确是否还需要额外的实际操作。"
                    )
                    return StepOutcome({}, next_prompt=next_prompt)
                
        if self._in_plan_mode():
            remaining = self._check_plan_completion()
            if remaining == 0:
                self._exit_plan_mode(); yield "[Info] Plan完成：plan.md中0个[ ]残留，退出plan模式。\n"
        
        yield "[Info] Final response to user.\n"
        return StepOutcome(response, next_prompt=None)
    
    def do_start_long_term_update(self, args, response):
        '''Agent觉得当前任务完成后有重要信息需要记忆时调用此工具。'''
        confirmation_rule = (
            "**受控自动结算**：当前处理器只能修改受限的顶层记忆文本，无需再次 ask_user；"
            "没有合格信息时不要写入。"
            if self.memory_only else
            "**确认与技术边界**：除非用户当前消息已经明确批准本次记忆更新，否则先用 `ask_user` 给出准确路径和动作。"
        )
        prompt = '''### [总结提炼经验] 既然你觉得当前任务有重要信息需要记忆，请提取最近一次任务中【事实验证成功且长期有效】的环境事实、用户偏好、重要步骤，更新记忆。
本工具是标记开启结算过程，若已在更新记忆过程或没有值得记忆的点，忽略本次调用。
**如果没有经验证的，未来能用上的信息，忽略本次调用！**
**只能提取行动验证成功的信息**：
- **环境事实**（路径/凭证/配置）→ `file_patch` 更新 L2，同步 L1
- **复杂任务经验**（关键坑点/前置条件/重要步骤）→ L3 精简 SOP（只记你被坑得多次重试的核心要点）
**禁止**：临时变量、具体推理过程、未验证信息、通用常识、你可以轻松复现的细节、只是做了但没有验证的信息
**操作**：严格遵循提供的L0的记忆更新SOP。先 `file_read` 看现有 → 判断类型 → 最小化更新 → 无新内容跳过，保证对记忆库最小局部修改。\n
''' + confirmation_rule + ''' 已有顶层 `.md`、L1/L2 只能 `file_patch`；仅可用 `file_write` 新建顶层 `.md`，禁止覆盖已有记忆；禁止用 `code_run` 修改记忆，禁止修改 `memory/*.py`、JSON、备份和 L4 原始会话。
''' + get_global_memory()
        yield "[Info] Start distilling good memory for long-term storage.\n"
        path = './memory/memory_management_sop.md'
        if os.path.exists(path): result = 'This is L0:\n' + file_read(path, show_linenos=False)
        else: result = "Memory Management SOP not found. Do not update memory."
        return StepOutcome(result, next_prompt=prompt)

    def _fold_earlier(self, lines):
        FALLBACK = '直接回答了用户问题'
        parts, cnt, last = [], 0, ''
        def flush():
            if cnt:
                if FALLBACK in last: parts.append(f'[Agent]（{cnt} turns）')
                else: parts.append(f'{last}（{cnt} turns）')
        for line in lines:
            if line.startswith('[USER]'):
                flush(); parts.append(line); cnt = 0; last = ''
            else: cnt += 1; last = line
        flush()
        return "\n".join(parts[-70:])

    def _get_anchor_prompt(self, skip=False):
        if skip: return "\n"
        h = self.history_info; W = 30
        earlier = f'<earlier_context>\n{self._fold_earlier(h[:-W])}\n</earlier_context>\n' if len(h) > W else ""
        h_str = "\n".join(h[-W:])
        prompt = f"\n### [WORKING MEMORY]\n{earlier}<history>\n{h_str}\n</history>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"
        if self.working.get('key_info'): prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"
        if self.working.get('related_sop'): prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"
        if getattr(self.parent, 'verbose', False): self.print(prompt)
        return prompt
    




    def do_jmcomic_download(self, args, response):
        """Download a comic album from JMComic by album ID.
Downloads all pages to temp/jmcomic/ and returns FILE refs."""
        album_id = str(args.get("album_id", ""))
        if not album_id:
            yield "[Error] album_id is required.\n"
            return StepOutcome({"status": "error", "msg": "Missing album_id"}, next_prompt="\n")

        import jmcomic
        import os

        download_dir = os.path.join(script_dir, "temp", "jmcomic", album_id)

        # Check if already downloaded
        if os.path.isdir(download_dir):
            existing = []
            for root, dirs, files in os.walk(download_dir):
                for f in sorted(files):
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        existing.append(os.path.join(root, f))
            if existing:
                n = len(existing)
                file_refs = "\n".join(f"[FILE:{p}]" for p in existing[:50])
                formatted = (f"✅ 漫画已在缓存中，共 {n} 张图片。\n" + file_refs)
                if n > 50:
                    formatted += f"\n...以及{n-50}张未列出"
                yield formatted + "\n"
                next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
                return StepOutcome(formatted, next_prompt=next_prompt)

        os.makedirs(download_dir, exist_ok=True)
        yield f"[Action] Downloading JMComic album {album_id}...\n"

        try:
            option = jmcomic.JmOption.default()
            option.download.threading.image = 8   # balanced: fast enough for 1.6GB server, won't OOM
            option.download.threading.photo = 1   # limit concurrent chapter downloads (was default 2)
            option.download.image.suffix = ".jpg"
            option.dir_rule.base_dir = download_dir

            album, downloader = jmcomic.download_album(album_id, option=option)

            image_paths = []
            album_title = getattr(album, "name", album_id)
            for photo in album:
                for img in photo:
                    p = getattr(img, "download_url", "")
                    if os.path.isfile(str(p)):
                        image_paths.append(p)

            if not image_paths:
                for root, dirs, files in os.walk(download_dir):
                    for f in sorted(files):
                        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                            image_paths.append(os.path.join(root, f))

            n = len(image_paths)
            yield f"[Status] OK Downloaded {n} images for album {album_id}: {album_title}\n"
            gc.collect()  # free JMComic download memory

            file_refs = "\n".join(f"[FILE:{p}]" for p in image_paths[:50])
            formatted = (f"\u4e0b\u8f7d\u5b8c\u6210\u3002\u6f2b\u753b\u300a{album_title}\u300b({album_id}) \u5171 {n} \u5f20\u56fe\u7247\u3002\n"
                         f"\u4ee5\u4e0b\u662f\u56fe\u7247\u6587\u4ef6\u5f15\u7528\uff1a\n{file_refs}\n")
            if n > 50:
                formatted += f"...\u4ee5\u53ca{n-50}\u5f20\u672a\u5217\u51fa\u7684\u56fe\u7247\n"

            next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
            return StepOutcome(formatted, next_prompt=next_prompt)

        except Exception as e:
            yield f"[Status] X Download failed: {e}\n"
            import traceback; traceback.print_exc()
            next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt=next_prompt)




    def do_doc_edit(self, args, response):
        """Edit .docx/.xlsx documents. Read, create, or edit office documents."""
        from frontends.shared.doc_ops import dispatch
        action = str(args.get("action", ""))
        path = self._get_abs_path(args.get("path", ""))
        dispatch_args = dict(args)
        dispatch_args["path"] = path
        if action in {"read", "edit"} and not _read_allowed(path):
            denied = _read_denied_result(path)
            result = {"ok": False, "result": denied["msg"]}
            yield "[Doc] FAIL: " + result["result"] + "\n"
            return StepOutcome(result, next_prompt=result["result"] + "\n")
        if action in {"create", "edit"}:
            raw_output = args.get("output_path") if action == "edit" else args.get("path")
            output_path = self._get_abs_path(raw_output or args.get("path", ""))
            if Path(output_path).is_symlink() or not _write_allowed(output_path):
                denied = _write_denied_result(output_path)
                result = {"ok": False, "result": denied["msg"]}
                yield "[Doc] FAIL: " + result["result"] + "\n"
                return StepOutcome(result, next_prompt=result["result"] + "\n")
            if action == "edit" and args.get("output_path"):
                dispatch_args["output_path"] = output_path
        result = dispatch(action, dispatch_args)
        s = result.get("result", "")
        if result.get("path"):
            s += "\n[FILE:" + result["path"] + "]"
        yield "[Doc] " + ("OK" if result["ok"] else "FAIL") + ": " + s + "\n"
        yield s + "\n"
        next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt if result["ok"] else ("Retry: " + result.get("result", "") + "\n" + next_prompt))


    def do_spawn_subagent(self, args, response):
        """Spawn a sub-agent for parallel task execution."""
        from frontends.shared.sub_agent import spawn
        r = spawn(args.get("task", ""), max_turns=args.get("max_turns", 30))
        if r["ok"]:
            yield "[SubAgent] " + r["reason"] + "\n"
        else:
            yield "[SubAgent] Failed: " + r.get("reason", "") + "\n"
        next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
        return StepOutcome(r, next_prompt=next_prompt)

    def do_list_subagents(self, args, response):
        """List all running sub-agents and their status."""
        from frontends.shared.sub_agent import list_subagents
        subs = list_subagents()
        if not subs:
            yield "[SubAgent] 当前没有活跃的子代理。\n"
            return StepOutcome({"subs": []}, next_prompt="\n")
        lines = ["当前子代理:"]
        for s in subs:
            alive = "🟢" if s["alive"] else "🔴"
            lines.append(f"{alive} {s['id']}: {s['status']} | turns={s['turns']} | {s['progress'][:60]}")
        text = "\n".join(lines)
        yield text + "\n"
        return StepOutcome({"subs": subs}, next_prompt="\n")

    def do_talk_subagent(self, args, response):
        """Send a message to a running sub-agent."""
        from frontends.shared.sub_agent import talk
        r = talk(args["id"], args["message"])
        yield "[SubAgent] " + r.get("reason", "") + "\n"
        next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
        return StepOutcome(r, next_prompt=next_prompt if r["ok"] else ("Retry: " + r.get("reason", "") + "\n" + next_prompt))

    def do_collect_subagent(self, args, response):
        """Collect results from a finished sub-agent and clean it up."""
        from frontends.shared.sub_agent import collect
        r = collect(args["id"])
        yield "[SubAgent] " + ("结果已收集" if r["ok"] else r.get("reason", "")) + "\n"
        if r.get("result"):
            yield "结果: " + r["result"][:500] + "\n"
        return StepOutcome(r, next_prompt="\n")

    def do_mcp_call(self, args, response):
        """Call an MCP server tool. Uses subprocess to invoke mcp-cli."""
        server = args.get("server", "")
        tool = args.get("tool", "")
        tool_args = args.get("args", {})
        if not server or not tool:
            yield "[MCP] ERROR: server and tool required.\n"
            return StepOutcome({"error": "server and tool required"}, next_prompt="\n")
        if not _mcp_tool_allowed(server, tool):
            error = f"{server}/{tool} not allowlisted by GENERICAGENT_MCP_ALLOWLIST"
            yield f"[MCP] ERROR: {error}\n"
            return StepOutcome({"error": error}, next_prompt="\n")
        try:
            import subprocess, json
            cmd = [sys.executable, "-m", "mcp_cli", "call", server, tool, json.dumps(tool_args)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                result = r.stdout.strip()[:2000]
                yield f"[MCP] {server}/{tool} OK\n"
            else:
                result = r.stderr.strip()[:500]
                yield f"[MCP] {server}/{tool} error: {result}\n"
            return StepOutcome({"result": result}, next_prompt="\n")
        except ImportError:
            yield "[MCP] mcp_cli not installed. Install: pip install mcp-cli\n"
            return StepOutcome({"error": "mcp_cli not installed"}, next_prompt="\n")
        except subprocess.TimeoutExpired:
            yield "[MCP] timeout\n"
            return StepOutcome({"error": "timeout"}, next_prompt="\n")
        except Exception as e:
            yield f"[MCP] error: {e}\n"
            return StepOutcome({"error": str(e)}, next_prompt="\n")

    def do_qq_group_op(self, args, response):
        """QQ 群管理操作：禁言/解禁/踢人/公告等。通过 NapCat WebSocket 执行。"""
        if not _env_flag_enabled("GENERICAGENT_QQ_ADMIN_ENABLED"):
            error = "QQ group administration disabled by GENERICAGENT_QQ_ADMIN_ENABLED"
            yield f"[QQGroup] ERROR: {error}\n"
            return StepOutcome({"error": error}, next_prompt="\n")
        op = args.get("op", "")
        user_id = args.get("user_id", "")
        group_id = args.get("group_id", "")
        duration = args.get("duration", 600)
        text = args.get("text", "")
        if not op:
            yield "[QQGroup] ERROR: op required (ban/unban/kick/notice)\n"
            return StepOutcome({"error": "op required"}, next_prompt="\n")
        if op not in {"ban", "unban", "kick", "notice"}:
            yield f"[QQGroup] Unknown op: {op}\n"
            return StepOutcome({"error": f"unknown op {op}"}, next_prompt="\n")
        if isinstance(group_id, bool) or not re.fullmatch(r"[0-9]+", str(group_id)):
            error = "group_id must be numeric"
            yield f"[QQGroup] ERROR: {error}\n"
            return StepOutcome({"error": error}, next_prompt="\n")
        if op in {"ban", "unban", "kick"} and (
            isinstance(user_id, bool) or not re.fullmatch(r"[0-9]+", str(user_id))
        ):
            error = f"user_id must be numeric for {op}"
            yield f"[QQGroup] ERROR: {error}\n"
            return StepOutcome({"error": error}, next_prompt="\n")
        if op == "ban" and (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 1 <= duration <= 2592000
        ):
            error = "duration must be an integer from 1 to 2592000 for ban"
            yield f"[QQGroup] ERROR: {error}\n"
            return StepOutcome({"error": error}, next_prompt="\n")
        if op == "notice" and (not isinstance(text, str) or not text.strip()):
            error = "text must be non-empty for notice"
            yield f"[QQGroup] ERROR: {error}\n"
            return StepOutcome({"error": error}, next_prompt="\n")
        group_id = int(group_id)
        if op in {"ban", "unban", "kick"}:
            user_id = int(user_id)
        # Map operations to NapCat API calls
        action_map = {
            "ban": ("set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": duration}),
            "unban": ("set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": 0}),
            "kick": ("set_group_kick", {"group_id": group_id, "user_id": user_id, "reject_add_request": False}),
            "notice": ("_send_group_notice", {"group_id": group_id, "content": text}),
        }
        action, params = action_map[op]
        try:
            import json, subprocess
            # Use aiohttp to call NapCat via WebSocket (local subprocess)
            ws_code = (
                "import asyncio, json, sys, aiohttp\npayload = json.loads(sys.argv[1])\nasync def main():\n"
                "  async with aiohttp.ClientSession() as s:\n"
                "    async with s.ws_connect('ws://127.0.0.1:3001/ws', timeout=5) as ws:\n"
                "      await ws.send_json(payload)\n"
                "      msg = await asyncio.wait_for(ws.receive(), timeout=5)\n"
                "      if msg.type == aiohttp.WSMsgType.TEXT:\n"
                "        print(json.loads(msg.data).get('status',''))\n"
                "asyncio.run(main())\n"
            )
            payload = json.dumps({"action": action, "params": params}, ensure_ascii=False)
            r = subprocess.run([sys.executable, '-c', ws_code, payload],
                             capture_output=True, text=True, timeout=8)
            result = (r.stdout.strip() or r.stderr.strip())[:200]
            yield f"[QQGroup] {op}: {result}\n"
            return StepOutcome({"result": result}, next_prompt="\n")
        except subprocess.TimeoutExpired:
            yield "[QQGroup] timeout\n"
            return StepOutcome({"error": "timeout"}, next_prompt="\n")
        except Exception as e:
            yield f"[QQGroup] error: {e}\n"
            return StepOutcome({"error": str(e)}, next_prompt="\n")

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        _c = re.sub(r'```.*?```|<thinking>.*?</thinking>', '', response.content, flags=re.DOTALL)
        rsumm = re.search(r"<summary>(.*?)</summary>", _c, re.DOTALL)
        if rsumm: summary = rsumm.group(1).strip()
        else:
            tc = tool_calls[0]
            clean_args = {k: v for k, v in tc['args'].items() if not k.startswith('_')}
            summary = _c.strip() or smart_format(
                "直接回答了用户问题"
                if tc['tool_name'] == 'no_tool'
                else f"{tc['tool_name']}, args: {clean_args}",
                max_str_len=40,
            )
            next_prompt += "\n\n\n[SYSTEM] 必须在回复文本中包含<summary>！\n\n"
        summary = smart_format(summary.replace('\n', ''), max_str_len=80)
        self.history_info.append(f'[Agent] {summary}')
        _plan = self._in_plan_mode()

        if turn % 75 == 0 and (not _plan):
            next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。必须总结情况进行ask_user，不允许继续重试。"
        elif turn % 7 == 0:
            next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。禁止无效重试。若无有效进展，必须切换策略：1. 探测物理边界 2. 请求用户协助。如有需要，可调用 update_working_checkpoint 保存关键上下文。"
        elif turn % 10 == 0: next_prompt += get_global_memory()

        if _plan and turn >= 10 and turn % 5 == 0:
            next_prompt = f"[Plan Hint] 正在计划模式。必须 file_read({_plan}) 确认当前步骤，回复开头引用：📌 当前步骤：...\n\n" + next_prompt
        if _plan and turn >= 120: next_prompt += f"\n\n[DANGER] Plan模式已运行 {turn} 轮，已达上限。必须 ask_user 汇报进度并确认是否继续。"

        injkeyinfo = consume_file(self.parent.task_dir, '_keyinfo')
        injprompt = consume_file(self.parent.task_dir, '_intervene')
        if injkeyinfo: self.working['key_info'] = self.working.get('key_info', '') + f"\n[MASTER] {injkeyinfo}"
        if injprompt: next_prompt += f"\n\n[MASTER] {injprompt}\n"
        for hook in list(getattr(self.parent, '_turn_end_hooks', {}).values()): hook(locals())  # current readonly
        return next_prompt

def get_global_memory():
    prompt = "\n"
    try:
        suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
        insight_path = os.path.join(script_dir, 'memory/global_mem_insight.txt')
        with open(insight_path, 'r', encoding='utf-8', errors='replace') as f: insight = f.read()
        validate_injected_memory(insight_path, insight)
        with open(os.path.join(script_dir, f'assets/insight_fixed_structure{suffix}.txt'), 'r', encoding='utf-8') as f: structure = f.read()
        prompt += f'cwd = {os.path.join(script_dir, "temp")} (./)\n'
        prompt += f"\n[Memory] (../memory)\n"
        prompt += structure + '\n../memory/global_mem_insight.txt:\n'
        prompt += insight + "\n"
        profile_path = os.path.join(script_dir, 'memory/personal_bootstrap_profile.md')
        with open(profile_path, 'r', encoding='utf-8', errors='replace') as f:
            profile = f.read()
        validate_injected_memory(profile_path, profile)
        prompt += f"\n[Profile] (../memory/personal_bootstrap_profile.md):\n" + profile + "\n"
        try:
            from skill_manager import render_skill_catalog
            catalog = render_skill_catalog(os.path.join(script_dir, "memory", "skills"))
            if catalog:
                prompt += "\n[Approved Skills] Read the matching SKILL.md only when relevant:\n" + catalog + "\n"
        except Exception as error:
            print(f"[WARN] Skill catalog unavailable: {error}")
    except FileNotFoundError: pass
    return prompt
