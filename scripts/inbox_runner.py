#!/usr/bin/env python3
"""GenericAgent inbox bridge with lightweight supervision.

Protocol:
- Put .txt/.md/.task/.json files in /opt/GenericAgent/sandbox/inbox.
- This script claims one pending item at a time, creates temp/inbox_* task dir,
  writes input.txt, and launches agentmain.py --task <absdir> --once.
- Completed inbox tasks are summarized under sandbox/reports/inbox_results/.
- Each completed task also gets a supervision audit under sandbox/reports/inbox_audits/.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path('/opt/GenericAgent')
INBOX = ROOT / 'sandbox' / 'inbox'
REPORTS = ROOT / 'sandbox' / 'reports' / 'inbox_results'
AUDITS = ROOT / 'sandbox' / 'reports' / 'inbox_audits'
TASK_ROOT = ROOT / 'temp'
CHARTER = ROOT / 'memory' / 'personal_assistant_charter.md'
PYTHON = ROOT / 'venv' / 'bin' / 'python'
AGENTMAIN = ROOT / 'agentmain.py'
LOG = ROOT / 'frontends' / 'temp' / 'inbox_runner.log'
CLAIM_SUFFIX = '.claimed'
ALLOWED_SUFFIXES = {'.txt', '.md', '.task'}
MAX_PROMPT_BYTES = 128 * 1024
MAX_ACTIVE_INBOX_TASKS = 1


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(line)


def slugify(name: str) -> str:
    base = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('._-')
    return (base or 'task')[:48]


def read_prompt(path: Path) -> str:
    if path.suffix.lower() == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        prompt = data.get('prompt') or data.get('task') or data.get('content')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('json inbox task must contain non-empty prompt/task/content string')
        title = data.get('title')
        if isinstance(title, str) and title.strip():
            return f"[Inbox Task] {title.strip()}\n\n{prompt.strip()}"
        return prompt.strip()
    return path.read_text(encoding='utf-8').strip()


def assistant_charter() -> str:
    if not CHARTER.exists():
        return ''
    text = CHARTER.read_text(encoding='utf-8', errors='replace').strip()
    if len(text) > 16 * 1024:
        return text[:16 * 1024] + '\n\n[Charter truncated by inbox_runner.py]'
    return text


def build_task_input(prompt: str) -> str:
    parts = []
    charter = assistant_charter()
    if charter:
        parts.extend([
            '[Personal Assistant Charter]',
            charter,
            '[/Personal Assistant Charter]',
            '',
        ])
    parts.extend([
        '[Inbox User Task]',
        prompt.strip(),
        '[/Inbox User Task]',
        '',
        '[System constraint] Treat this as an inbox task. Write any generated artifacts under /opt/GenericAgent/sandbox unless the user explicitly requested another approved location.',
    ])
    return '\n'.join(parts).rstrip() + '\n'


def pending_files() -> list[Path]:
    INBOX.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(INBOX.iterdir(), key=lambda x: x.stat().st_mtime):
        if not p.is_file():
            continue
        if p.name.startswith('.') or p.name.endswith(CLAIM_SUFFIX):
            continue
        if p.suffix.lower() in ALLOWED_SUFFIXES or p.suffix.lower() == '.json':
            out.append(p)
    return out


def active_inbox_tasks() -> list[Path]:
    active = []
    for d in TASK_ROOT.glob('inbox_*'):
        if not d.is_dir():
            continue
        if (d / 'done.json').exists():
            continue
        pid_file = d / 'pid'
        if pid_file.exists():
            pid = pid_file.read_text(encoding='utf-8', errors='ignore').strip()
            if pid.isdigit() and Path(f'/proc/{pid}').exists():
                active.append(d)
                continue
        if time.time() - d.stat().st_mtime < 24 * 3600:
            active.append(d)
    return active


def _extract_paths(text: str) -> list[str]:
    patterns = [
        r'\[FILE:([^\]]+)\]',
        r'生成文件[:：]\s*([^\s`]+)',
        r'保存(?:到|至)\s*([^\s`]+)',
    ]
    paths: list[str] = []
    for pat in patterns:
        paths.extend(m.strip().strip('。,.，') for m in re.findall(pat, text or ''))
    return [p for p in paths if p]


def audit_task(task_dir: Path, meta: dict, body: str) -> dict:
    checks: list[dict] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({'name': name, 'status': status, 'detail': detail})

    if meta.get('status') == 'completed' and meta.get('exit_code') == 0:
        add('completion', 'PASS', 'done.json reports completed with exit_code=0')
    else:
        add('completion', 'FAIL', f"status={meta.get('status')} exit_code={meta.get('exit_code')} error={meta.get('error')}")

    rounds = meta.get('rounds')
    if isinstance(rounds, int) and rounds > 0:
        add('rounds', 'PASS', f'rounds={rounds}')
    else:
        add('rounds', 'PARTIAL', f'rounds value is weak or missing: {rounds!r}')

    if body.strip() and '[ROUND END]' in body:
        add('output', 'PASS', 'output.txt exists and contains [ROUND END]')
    elif body.strip():
        add('output', 'PARTIAL', 'output.txt exists but lacks [ROUND END] marker')
    else:
        add('output', 'FAIL', 'output.txt missing or empty')

    suspicious = []
    for token in ('Traceback', 'Backend Error', 'Exception:', 'SyntaxError', 'Permission denied'):
        if token.lower() in body.lower():
            suspicious.append(token)
    if suspicious:
        add('runtime_errors', 'FAIL', 'suspicious runtime text: ' + ', '.join(suspicious))
    else:
        add('runtime_errors', 'PASS', 'no traceback/backend-error markers in output')

    stderr = task_dir / 'stderr.log'
    launcher_stderr = task_dir / 'launcher.stderr.log'
    stderr_text = ''
    for p in (stderr, launcher_stderr):
        if p.exists():
            stderr_text += p.read_text(encoding='utf-8', errors='replace')
    if 'Traceback' in stderr_text or 'SyntaxError' in stderr_text:
        add('stderr', 'FAIL', 'stderr contains Python error markers')
    else:
        add('stderr', 'PASS', 'no Python error markers in stderr logs')

    unsafe_paths = []
    for raw in _extract_paths(body):
        try:
            p = Path(raw)
            if p.is_absolute() and not (p == ROOT / 'sandbox' or ROOT / 'sandbox' in p.parents):
                unsafe_paths.append(raw)
        except Exception:
            continue
    if unsafe_paths:
        add('artifact_boundary', 'FAIL', 'artifact paths outside sandbox: ' + ', '.join(unsafe_paths[:5]))
    else:
        add('artifact_boundary', 'PASS', 'no generated artifact paths outside sandbox detected')

    if 'Write denied outside allowed roots' in body:
        add('write_boundary', 'PARTIAL', 'a write attempt was blocked by sandbox policy; task may need sandbox path guidance')
    else:
        add('write_boundary', 'PASS', 'no sandbox write denial detected')

    statuses = {c['status'] for c in checks}
    verdict = 'FAIL' if 'FAIL' in statuses else ('PARTIAL' if 'PARTIAL' in statuses else 'PASS')
    return {
        'task': task_dir.name,
        'task_dir': str(task_dir),
        'audited_at': datetime.now().isoformat(timespec='seconds'),
        'verdict': verdict,
        'checks': checks,
    }


def write_audit(task_dir: Path, audit: dict) -> tuple[Path, Path]:
    AUDITS.mkdir(parents=True, exist_ok=True)
    json_path = AUDITS / f'{task_dir.name}.json'
    md_path = AUDITS / f'{task_dir.name}.md'
    json_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = [
        f"# Inbox Supervision: {task_dir.name}",
        '',
        f"- verdict: `{audit['verdict']}`",
        f"- audited_at: `{audit['audited_at']}`",
        f"- task_dir: `{task_dir}`",
        '',
        '## Checks',
        '',
    ]
    for c in audit['checks']:
        lines.append(f"- `{c['status']}` **{c['name']}**: {c['detail']}")
    lines.append('')
    md_path.write_text('\n'.join(lines), encoding='utf-8')
    return json_path, md_path


def summarize_completed() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    AUDITS.mkdir(parents=True, exist_ok=True)
    for d in sorted(TASK_ROOT.glob('inbox_*')):
        if not d.is_dir():
            continue
        done = d / 'done.json'
        if not done.exists():
            continue
        try:
            meta = json.loads(done.read_text(encoding='utf-8'))
        except Exception as e:
            meta = {'status': 'error', 'exit_code': 1, 'error': f'cannot parse done.json: {e}'}
        output = d / 'output.txt'
        body = output.read_text(encoding='utf-8', errors='replace') if output.exists() else ''

        audit_marker = d / '.inbox_audited'
        audit_path = audit_marker.read_text(encoding='utf-8').strip() if audit_marker.exists() else ''
        if not audit_path or not Path(audit_path).exists():
            audit = audit_task(d, meta, body)
            _, audit_md = write_audit(d, audit)
            audit_marker.write_text(str(audit_md), encoding='utf-8')
            audit_path = str(audit_md)
            log(f'audited {d.name}: {audit["verdict"]} -> {audit_md}')
        else:
            audit = json.loads((AUDITS / f'{d.name}.json').read_text(encoding='utf-8')) if (AUDITS / f'{d.name}.json').exists() else {'verdict': 'UNKNOWN'}

        marker = d / '.inbox_reported'
        if marker.exists():
            continue
        title = d.name
        report = REPORTS / f'{d.name}.md'
        report.write_text(
            f"# Inbox Result: {title}\n\n"
            f"- status: `{meta.get('status')}`\n"
            f"- exit_code: `{meta.get('exit_code')}`\n"
            f"- finished_at: `{meta.get('finished_at')}`\n"
            f"- task_dir: `{d}`\n"
            f"- supervision: `{audit.get('verdict')}`\n"
            f"- audit_report: `{audit_path}`\n"
            f"- error: `{meta.get('error')}`\n\n"
            f"## Output\n\n{body}\n",
            encoding='utf-8',
        )
        marker.write_text(str(report), encoding='utf-8')
        log(f'reported {d.name} -> {report}')


def claim_and_launch(path: Path) -> Path | None:
    size = path.stat().st_size
    if size > MAX_PROMPT_BYTES:
        bad = INBOX / f'{path.name}.rejected'
        path.rename(bad)
        log(f'rejected {path.name}: too large ({size} bytes)')
        return None
    claimed = path.with_name(path.name + CLAIM_SUFFIX)
    path.rename(claimed)
    try:
        prompt = read_prompt(claimed)
        if not prompt:
            raise ValueError('empty prompt')
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        task_name = f'inbox_{ts}_{slugify(path.stem)}'
        task_dir = TASK_ROOT / task_name
        task_dir.mkdir(parents=True, exist_ok=False)
        shutil.move(str(claimed), str(task_dir / f'source_{path.name}'))
        (task_dir / 'input.txt').write_text(build_task_input(prompt), encoding='utf-8')
        proc = subprocess.run(
            [str(PYTHON), str(AGENTMAIN), '--task', str(task_dir), '--once'],
            cwd=str(ROOT), text=True, capture_output=True, timeout=30,
        )
        (task_dir / 'launcher.stdout.log').write_text(proc.stdout, encoding='utf-8')
        (task_dir / 'launcher.stderr.log').write_text(proc.stderr, encoding='utf-8')
        if proc.returncode != 0:
            log(f'launch command returned {proc.returncode} for {task_dir.name}: {proc.stderr[:200]}')
        else:
            log(f'launched {task_dir.name}: {proc.stdout.strip()}')
        return task_dir
    except Exception as e:
        failed = INBOX / f'{path.name}.failed'
        if claimed.exists():
            claimed.rename(failed)
        log(f'failed to claim {path.name}: {e}')
        return None


def main() -> int:
    summarize_completed()
    active = active_inbox_tasks()
    if len(active) >= MAX_ACTIVE_INBOX_TASKS:
        log(f'skip launch: active inbox task exists ({active[0].name})')
        return 0
    files = pending_files()
    if not files:
        return 0
    claim_and_launch(files[0])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
