#!/usr/bin/env python3
"""Generate a GenericAgent assistant command-center dashboard."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path('/opt/GenericAgent')
REPORT_DIR = ROOT / 'sandbox' / 'reports'
DASHBOARD = REPORT_DIR / 'assistant_dashboard.md'
DASHBOARD_JSON = REPORT_DIR / 'assistant_dashboard.json'
INBOX = ROOT / 'sandbox' / 'inbox'
INBOX_RESULTS = REPORT_DIR / 'inbox_results'
INBOX_AUDITS = REPORT_DIR / 'inbox_audits'
CHARTER = ROOT / 'memory' / 'personal_assistant_charter.md'
SUPERVISION = REPORT_DIR / 'supervision' / 'current.json'


def run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout)
        out = (p.stdout or '') + (('\n' + p.stderr) if p.stderr else '')
        return p.returncode, out.strip()
    except Exception as e:
        return 999, f'{type(e).__name__}: {e}'


def systemctl_active(unit: str) -> bool:
    code, _ = run(['systemctl', 'is-active', '--quiet', unit], timeout=5)
    return code == 0


def latest_files(path: Path, pattern: str = '*', n: int = 5) -> list[Path]:
    if not path.exists():
        return []
    files = [p for p in path.glob(pattern) if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:n]


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        return {'_error': str(e)}


def collect() -> dict:
    services = {
        'genericagent.service': systemctl_active('genericagent.service'),
        'genericagent-napcat.service': systemctl_active('genericagent-napcat.service'),
        'genericagent-qq.service': systemctl_active('genericagent-qq.service'),
        'genericagent-port-guard.service': systemctl_active('genericagent-port-guard.service'),
        'genericagent-watchdog.timer': systemctl_active('genericagent-watchdog.timer'),
        'genericagent-inbox.timer': systemctl_active('genericagent-inbox.timer'),
        'genericagent-dashboard.timer': systemctl_active('genericagent-dashboard.timer'),
    }
    _, timers = run(['systemctl', 'list-timers', '--all', '--no-pager'], timeout=10)
    _, listeners = run(['bash', '-lc', "ss -ltnp | grep -E '127\\.0\\.0\\.1:(3001|6099)|0\\.0\\.0\\.0:(3001|6099)|\\*:(3001|6099)' || true"], timeout=10)
    _, status = run([str(ROOT / 'scripts' / 'status.sh')], timeout=40)
    pending = sorted([p.name for p in INBOX.iterdir() if p.is_file() and p.suffix in {'.md', '.txt', '.task', '.json'}]) if INBOX.exists() else []
    active_tasks = []
    for d in sorted((ROOT / 'temp').glob('inbox_*'), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir() and not (d / 'done.json').exists():
            active_tasks.append(str(d))
    latest_results = latest_files(INBOX_RESULTS, '*.md', 5)
    latest_audits_json = latest_files(INBOX_AUDITS, '*.json', 10)
    audits = []
    for p in latest_audits_json:
        data = load_json(p)
        data['_path'] = str(p)
        audits.append(data)
    failed_or_partial = [a for a in audits if a.get('verdict') in {'FAIL', 'PARTIAL'}]
    _, git_short = run(['git', 'status', '--short', 'scripts/assistantctl.py', 'scripts/assistant_dashboard.py', 'scripts/assistant_supervisor.py', 'scripts/inbox_runner.py', 'scripts/status.sh', 'scripts/submit_task.sh', 'scripts/watchdog.sh'], timeout=10)
    charter_state = {
        'path': str(CHARTER),
        'exists': CHARTER.exists(),
        'updated_at': datetime.fromtimestamp(CHARTER.stat().st_mtime).isoformat(timespec='seconds') if CHARTER.exists() else None,
    }
    supervision_state = load_json(SUPERVISION) if SUPERVISION.exists() else {'overall': 'NONE'}
    return {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'services': services,
        'assistant_charter': charter_state,
        'supervision': supervision_state,
        'timers_excerpt': '\n'.join([line for line in timers.splitlines() if 'genericagent-' in line or line.startswith('NEXT')]),
        'listeners': listeners,
        'status_excerpt': status,
        'pending_inbox': pending,
        'active_inbox_tasks': active_tasks,
        'latest_results': [str(p) for p in latest_results],
        'latest_audits': audits[:5],
        'failed_or_partial_audits': failed_or_partial,
        'git_short_scripts': git_short,
    }


def badge(ok: bool) -> str:
    return 'OK' if ok else 'FAIL'


def render(data: dict) -> str:
    services = data['services']
    all_core_ok = all(services.values())
    latest_verdict = data['latest_audits'][0].get('verdict') if data['latest_audits'] else 'NONE'
    supervisor_ok = data.get('supervision', {}).get('overall') in {'READY', 'NONE'}
    health = 'READY' if all_core_ok and not data['failed_or_partial_audits'] and supervisor_ok else 'NEEDS_ATTENTION'
    lines = [
        '# GenericAgent Assistant Dashboard',
        '',
        f"- generated_at: `{data['generated_at']}`",
        f"- overall: `{health}`",
        f"- latest_inbox_supervision: `{latest_verdict}`",
        '',
        '## Service State',
        '',
    ]
    for unit, ok in services.items():
        lines.append(f"- `{badge(ok)}` {unit}")
    lines += ['', '## Inbox', '']
    charter = data.get('assistant_charter', {})
    lines.append(f"- assistant_charter: `{'OK' if charter.get('exists') else 'MISSING'}` `{charter.get('path')}`")
    if charter.get('updated_at'):
        lines.append(f"- charter_updated_at: `{charter.get('updated_at')}`")
    lines.append(f"- pending_files: `{len(data['pending_inbox'])}`")
    if data['pending_inbox']:
        for name in data['pending_inbox'][:10]:
            lines.append(f"- pending: `{name}`")
    lines.append(f"- active_tasks: `{len(data['active_inbox_tasks'])}`")
    for task in data['active_inbox_tasks'][:5]:
        lines.append(f"- active: `{task}`")
    lines += ['', '## Recent Inbox Results', '']
    if data['latest_results']:
        for p in data['latest_results']:
            lines.append(f"- `{p}`")
    else:
        lines.append('- none')
    lines += ['', '## Supervision', '']
    supervision = data.get('supervision', {})
    lines.append(f"- supervisor_overall: `{supervision.get('overall', 'NONE')}`")
    if supervision.get('generated_at'):
        lines.append(f"- supervisor_generated_at: `{supervision.get('generated_at')}`")
    if data['latest_audits']:
        for audit in data['latest_audits']:
            lines.append(f"- `{audit.get('verdict', 'UNKNOWN')}` {audit.get('task', '?')} -> `{audit.get('_path')}`")
    else:
        lines.append('- no audits yet')
    if data['failed_or_partial_audits']:
        lines += ['', '## Attention Needed', '']
        for audit in data['failed_or_partial_audits'][:5]:
            lines.append(f"- `{audit.get('verdict')}` {audit.get('task')} -> `{audit.get('_path')}`")
    lines += [
        '',
        '## Timers',
        '',
        '```text',
        data['timers_excerpt'] or '(none)',
        '```',
        '',
        '## Listener Boundary',
        '',
        '```text',
        data['listeners'] or '(no matching listeners)',
        '```',
        '',
        '## How To Use Me',
        '',
        '```bash',
        'genericagent-assistant doctor --refresh',
        'genericagent-assistant alerts --verbose',
        'genericagent-assistant status',
        'genericagent-assistant submit --now "你的任务"',
        'genericagent-assistant results --latest --tail 80',
        'genericagent-assistant audits --latest',
        'genericagent-assistant dashboard --refresh --tail 120',
        'genericagent-assistant supervise --refresh --tail 120',
        '```',
        '',
        '## Script Worktree Note',
        '',
        '```text',
        data['git_short_scripts'] or '(clean for tracked script paths; untracked scripts may still exist)',
        '```',
        '',
    ]
    return '\n'.join(lines)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    data = collect()
    DASHBOARD_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    DASHBOARD.write_text(render(data), encoding='utf-8')
    print(DASHBOARD)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
