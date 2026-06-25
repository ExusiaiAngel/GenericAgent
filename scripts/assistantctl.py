#!/usr/bin/env python3
"""Unified operator CLI for the GenericAgent personal assistant stack."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path('/opt/GenericAgent')
PYTHON = ROOT / 'venv' / 'bin' / 'python'
SCRIPTS = ROOT / 'scripts'
REPORTS = ROOT / 'sandbox' / 'reports'
INBOX = ROOT / 'sandbox' / 'inbox'
RESULTS = REPORTS / 'inbox_results'
AUDITS = REPORTS / 'inbox_audits'
DASHBOARD = REPORTS / 'assistant_dashboard.md'
DASHBOARD_JSON = REPORTS / 'assistant_dashboard.json'
SUPERVISION = REPORTS / 'supervision' / 'current.md'
SUPERVISION_JSON = REPORTS / 'supervision' / 'current.json'
CORE_UNITS = [
    'genericagent.service',
    'genericagent-napcat.service',
    'genericagent-qq.service',
    'genericagent-port-guard.service',
    'genericagent-watchdog.timer',
    'genericagent-inbox.timer',
    'genericagent-dashboard.timer',
    'genericagent-supervisor.timer',
]


def run(cmd: list[str], timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout)
    if check and p.returncode != 0:
        sys.stderr.write(p.stderr or p.stdout)
        raise SystemExit(p.returncode)
    return p


def print_file(path: Path, lines: int | None = None) -> None:
    if not path.exists():
        raise SystemExit(f'not found: {path}')
    text = path.read_text(encoding='utf-8', errors='replace')
    if lines is not None:
        text = '\n'.join(text.splitlines()[-lines:])
    print(text)


def audit_files() -> list[Path]:
    return sorted([p for p in AUDITS.glob('*.json') if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True) if AUDITS.exists() else []


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        data['_path'] = str(path)
        return data
    except Exception as e:
        return {'verdict': 'BROKEN', 'task': path.stem, '_path': str(path), '_error': str(e)}


def active_units() -> dict[str, bool]:
    out = {}
    for unit in CORE_UNITS:
        out[unit] = run(['systemctl', 'is-active', '--quiet', unit], timeout=8).returncode == 0
    return out


def pending_and_active() -> tuple[list[Path], list[Path]]:
    pending = sorted([p for p in INBOX.iterdir() if p.is_file() and p.suffix in {'.md', '.txt', '.task', '.json'}]) if INBOX.exists() else []
    active = []
    for d in sorted((ROOT / 'temp').glob('inbox_*'), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir() and not (d / 'done.json').exists():
            active.append(d)
    return pending, active


def non_pass_audits(limit: int = 20) -> list[dict]:
    items = [load_json(p) for p in audit_files()]
    return [a for a in items if a.get('verdict') in {'FAIL', 'PARTIAL', 'BROKEN'}][:limit]


def supervisor_state() -> dict:
    if not SUPERVISION_JSON.exists():
        return {'overall': 'MISSING', 'findings': [], '_path': str(SUPERVISION_JSON)}
    data = load_json(SUPERVISION_JSON)
    if not isinstance(data.get('findings'), list):
        data['findings'] = []
    return data


def cmd_status(_args: argparse.Namespace) -> int:
    p = run([str(SCRIPTS / 'status.sh')], timeout=60)
    print(p.stdout, end='')
    if p.stderr:
        print(p.stderr, file=sys.stderr, end='')
    return p.returncode


def cmd_submit(args: argparse.Namespace) -> int:
    if args.file:
        p = run([str(SCRIPTS / 'submit_task.sh'), '--file', args.file], timeout=30)
    else:
        text = ' '.join(args.text).strip()
        if not text:
            raise SystemExit('submit requires task text or --file')
        p = run([str(SCRIPTS / 'submit_task.sh'), text], timeout=30)
    if p.returncode != 0:
        print(p.stderr or p.stdout, file=sys.stderr, end='')
        return p.returncode
    print(p.stdout.strip())
    if args.now:
        q = run([str(PYTHON), str(SCRIPTS / 'inbox_runner.py')], timeout=60)
        if q.stdout:
            print(q.stdout, end='')
        if q.stderr:
            print(q.stderr, file=sys.stderr, end='')
        return q.returncode
    return 0


def cmd_run_inbox(_args: argparse.Namespace) -> int:
    p = run([str(PYTHON), str(SCRIPTS / 'inbox_runner.py')], timeout=120)
    print(p.stdout, end='')
    if p.stderr:
        print(p.stderr, file=sys.stderr, end='')
    return p.returncode


def cmd_dashboard(args: argparse.Namespace) -> int:
    if args.refresh:
        p = run([str(PYTHON), str(SCRIPTS / 'assistant_dashboard.py')], timeout=60)
        if p.returncode != 0:
            print(p.stderr or p.stdout, file=sys.stderr, end='')
            return p.returncode
    print_file(DASHBOARD_JSON if args.json else DASHBOARD, lines=args.tail if not args.json else None)
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    if args.refresh:
        p = run([str(PYTHON), str(SCRIPTS / 'assistant_supervisor.py')], timeout=80)
        if p.returncode != 0 and args.fail_on_attention:
            print(p.stdout, end='')
            if p.stderr:
                print(p.stderr, file=sys.stderr, end='')
            return p.returncode
    print_file(SUPERVISION_JSON if args.json else SUPERVISION, lines=args.tail if not args.json else None)
    if args.fail_on_attention:
        data = load_json(SUPERVISION_JSON)
        return 0 if data.get('overall') == 'READY' else 1
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    files = sorted([p for p in RESULTS.glob('*.md') if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True) if RESULTS.exists() else []
    if args.latest:
        p = files[0] if files else None
        if not p:
            raise SystemExit('no inbox result reports found')
        print_file(p, lines=args.tail)
        return 0
    for p in files[:args.limit]:
        print(f'{p.stat().st_mtime:.0f}\t{p}')
    return 0


def cmd_audits(args: argparse.Namespace) -> int:
    files = audit_files()
    if args.latest:
        p = files[0] if files else None
        if not p:
            raise SystemExit('no inbox audit reports found')
        if args.json:
            print_file(p)
        else:
            md = p.with_suffix('.md')
            print_file(md if md.exists() else p, lines=args.tail)
        return 0
    for p in files[:args.limit]:
        d = load_json(p)
        print(f"{d.get('verdict','UNKNOWN')}\t{d.get('task','?')}\t{p}")
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    alerts = non_pass_audits(args.limit)
    supervisor = supervisor_state()
    supervisor_alert = supervisor.get('overall') not in {'READY'}
    if not alerts and not supervisor_alert:
        print('alerts=0')
        return 0
    print(f'alerts={len(alerts) + (1 if supervisor_alert else 0)}')
    if supervisor_alert:
        print(f"SUPERVISOR\t{supervisor.get('overall')}\t{SUPERVISION_JSON}")
        if args.verbose:
            for f in supervisor.get('findings', []):
                print(f"  - {f.get('severity')} {f.get('area')}: {f.get('detail')}")
    for a in alerts:
        print(f"{a.get('verdict')}\t{a.get('task')}\t{a.get('_path')}")
        if args.verbose:
            for c in a.get('checks', []):
                if c.get('status') != 'PASS':
                    print(f"  - {c.get('status')} {c.get('name')}: {c.get('detail')}")
    return 1 if args.fail_on_alert else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    if args.refresh:
        run([str(PYTHON), str(SCRIPTS / 'assistant_supervisor.py')], timeout=80)
        run([str(PYTHON), str(SCRIPTS / 'assistant_dashboard.py')], timeout=60)
        run([str(PYTHON), str(SCRIPTS / 'inbox_runner.py')], timeout=120)
    units = active_units()
    pending, active = pending_and_active()
    alerts = non_pass_audits(10)
    supervisor = supervisor_state()
    supervisor_ok = supervisor.get('overall') == 'READY'
    latest_audit = load_json(audit_files()[0]) if audit_files() else None
    overall = 'READY' if all(units.values()) and not pending and not active and not alerts and supervisor_ok else 'NEEDS_ATTENTION'
    print(f'overall={overall}')
    print('services=' + ('OK' if all(units.values()) else 'CHECK'))
    for unit, ok in units.items():
        print(f"  {'OK' if ok else 'FAIL'}\t{unit}")
    print(f'pending={len(pending)} active={len(active)} alerts={len(alerts)}')
    print(f"supervisor={supervisor.get('overall')} findings={len(supervisor.get('findings', []))}")
    if latest_audit:
        print(f"latest_supervision={latest_audit.get('verdict')} task={latest_audit.get('task')}")
    if not supervisor_ok:
        print('next_actions:')
        print('  - genericagent-assistant supervise --refresh --tail 120')
        print('  - inspect /opt/GenericAgent/sandbox/reports/supervision/current.md')
    elif alerts:
        print('next_actions:')
        print('  - genericagent-assistant alerts --verbose')
        print('  - genericagent-assistant audits --latest')
        print('  - inspect the failing task_dir from the audit report')
    elif pending or active:
        print('next_actions:')
        print('  - genericagent-assistant pending')
        print('  - genericagent-assistant run-inbox')
    else:
        print('next_actions:')
        print('  - genericagent-assistant submit --now "你的任务"')
        print('  - genericagent-assistant dashboard --refresh --tail 80')
    return 0 if overall == 'READY' else 1 if args.fail_on_attention else 0


def cmd_pending(_args: argparse.Namespace) -> int:
    pending, active = pending_and_active()
    print(f'pending={len(pending)}')
    for p in pending:
        print(f'pending\t{p}')
    print(f'active={len(active)}')
    for d in active:
        print(f'active\t{d}')
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    mapping = {
        'inbox': ROOT / 'frontends' / 'temp' / 'inbox_runner.log',
        'watchdog': ROOT / 'frontends' / 'temp' / 'watchdog.log',
        'napcat': ROOT / 'frontends' / 'temp' / 'napcat_qqapp.log',
    }
    print_file(mapping[args.name], lines=args.lines)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='assistantctl', description='GenericAgent assistant operator CLI')
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('status', help='show assistant status'); s.set_defaults(func=cmd_status)
    s = sub.add_parser('submit', help='submit an inbox task'); s.add_argument('text', nargs='*'); s.add_argument('--file'); s.add_argument('--now', action='store_true'); s.set_defaults(func=cmd_submit)
    s = sub.add_parser('run-inbox', help='run inbox runner once'); s.set_defaults(func=cmd_run_inbox)
    s = sub.add_parser('dashboard', help='print assistant dashboard'); s.add_argument('--refresh', action='store_true'); s.add_argument('--json', action='store_true'); s.add_argument('--tail', type=int); s.set_defaults(func=cmd_dashboard)
    s = sub.add_parser('supervise', help='record and print persistent supervisor snapshot'); s.add_argument('--refresh', action='store_true'); s.add_argument('--json', action='store_true'); s.add_argument('--tail', type=int); s.add_argument('--fail-on-attention', action='store_true'); s.set_defaults(func=cmd_supervise)
    s = sub.add_parser('results', help='list or show inbox results'); s.add_argument('--latest', action='store_true'); s.add_argument('--limit', type=int, default=10); s.add_argument('--tail', type=int); s.set_defaults(func=cmd_results)
    s = sub.add_parser('audits', help='list or show inbox supervision audits'); s.add_argument('--latest', action='store_true'); s.add_argument('--json', action='store_true'); s.add_argument('--limit', type=int, default=10); s.add_argument('--tail', type=int); s.set_defaults(func=cmd_audits)
    s = sub.add_parser('alerts', help='show FAIL/PARTIAL/BROKEN supervision audits'); s.add_argument('--limit', type=int, default=20); s.add_argument('--verbose', action='store_true'); s.add_argument('--fail-on-alert', action='store_true'); s.set_defaults(func=cmd_alerts)
    s = sub.add_parser('doctor', help='summarize assistant health and recommended next actions'); s.add_argument('--refresh', action='store_true'); s.add_argument('--fail-on-attention', action='store_true'); s.set_defaults(func=cmd_doctor)
    s = sub.add_parser('pending', help='show pending and active inbox tasks'); s.set_defaults(func=cmd_pending)
    s = sub.add_parser('logs', help='show recent logs'); s.add_argument('name', choices=['inbox', 'watchdog', 'napcat']); s.add_argument('--lines', type=int, default=80); s.set_defaults(func=cmd_logs)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == '__main__':
    raise SystemExit(main())
