#!/usr/bin/env python3
"""Persistent GenericAgent supervisor snapshots.

This script records an auditable health snapshot of the assistant stack and
writes a current alert file when anything needs attention.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path('/opt/GenericAgent')
REPORT_DIR = ROOT / 'sandbox' / 'reports'
SUPERVISION_DIR = REPORT_DIR / 'supervision'
CURRENT_JSON = SUPERVISION_DIR / 'current.json'
CURRENT_MD = SUPERVISION_DIR / 'current.md'
ALERT_MD = SUPERVISION_DIR / 'alert.md'
HISTORY_JSONL = SUPERVISION_DIR / 'history.jsonl'
INBOX = ROOT / 'sandbox' / 'inbox'
AUDITS = REPORT_DIR / 'inbox_audits'
PYTHON = ROOT / 'venv' / 'bin' / 'python'
DASHBOARD = ROOT / 'scripts' / 'assistant_dashboard.py'

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


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout)
        out = (p.stdout or '') + (('\n' + p.stderr) if p.stderr else '')
        return p.returncode, out.strip()
    except Exception as e:
        return 999, f'{type(e).__name__}: {e}'


def unit_state(unit: str) -> str:
    code, _ = run(['systemctl', 'is-active', '--quiet', unit], timeout=8)
    return 'active' if code == 0 else 'inactive'


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        return {'_error': str(e), '_path': str(path)}


def latest_audits(limit: int = 10) -> list[dict]:
    if not AUDITS.exists():
        return []
    files = sorted([p for p in AUDITS.glob('*.json') if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        data = load_json(p)
        data['_path'] = str(p)
        out.append(data)
    return out


def onebot_health() -> dict:
    code, out = run([str(PYTHON), '-c', r'''
import asyncio, json, time
import aiohttp
async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect('ws://127.0.0.1:3001/ws', timeout=8) as ws:
            echo='supervisor_'+str(int(time.time()*1000))
            await ws.send_json({'action':'get_status','params':{},'echo':echo})
            deadline=time.time()+8
            while time.time()<deadline:
                msg=await ws.receive(timeout=max(0.1, deadline-time.time()))
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data=json.loads(msg.data)
                    if data.get('echo') == echo:
                        d=data.get('data') or {}
                        print(json.dumps({'online': d.get('online'), 'good': d.get('good')}, ensure_ascii=False))
                        return 0
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    return 2
            return 3
raise SystemExit(asyncio.run(main()))
'''], timeout=15)
    if code != 0:
        return {'ok': False, 'detail': out or f'exit={code}'}
    data = load_json_from_text(out)
    return {'ok': bool(data.get('online') and data.get('good')), 'detail': data}


def load_json_from_text(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        return {'raw': text}


def listener_boundary() -> dict:
    code, out = run(['bash', '-lc', "ss -ltnp | grep -E '127\\.0\\.0\\.1:(3001|6099)|0\\.0\\.0\\.0:(3001|6099)|\\*:(3001|6099)' || true"], timeout=10)
    public = bool(run(['bash', '-lc', "ss -ltnp | grep -Eq '(^|[[:space:]])(0\\.0\\.0\\.0|\\*):3001|(^|[[:space:]])(0\\.0\\.0\\.0|\\*):6099'"], timeout=10)[0] == 0)
    return {'ok': code == 0 and not public, 'public': public, 'listeners': out}


def pending_and_active() -> tuple[list[str], list[str]]:
    pending = sorted([p.name for p in INBOX.iterdir() if p.is_file() and p.suffix in {'.md', '.txt', '.task', '.json'}]) if INBOX.exists() else []
    active = []
    for d in sorted((ROOT / 'temp').glob('inbox_*'), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir() and not (d / 'done.json').exists():
            active.append(str(d))
    return pending, active


def collect() -> dict:
    run([str(PYTHON), str(DASHBOARD)], timeout=60)
    units = {unit: unit_state(unit) for unit in CORE_UNITS}
    pending, active = pending_and_active()
    audits = latest_audits()
    non_pass = [a for a in audits if a.get('verdict') in {'FAIL', 'PARTIAL', 'BROKEN'}]
    boundary = listener_boundary()
    onebot = onebot_health()
    findings: list[dict] = []

    for unit, state in units.items():
        if state != 'active':
            findings.append({'severity': 'critical', 'area': 'service', 'detail': f'{unit} is {state}'})
    if pending:
        findings.append({'severity': 'warning', 'area': 'inbox', 'detail': f'{len(pending)} pending inbox task(s)'})
    if active:
        findings.append({'severity': 'warning', 'area': 'inbox', 'detail': f'{len(active)} active inbox task(s)'})
    for audit in non_pass[:5]:
        findings.append({'severity': 'critical', 'area': 'audit', 'detail': f"{audit.get('verdict')} {audit.get('task')} {audit.get('_path')}"})
    if not boundary['ok']:
        findings.append({'severity': 'critical', 'area': 'security', 'detail': 'NapCat listener boundary is not local-only'})
    if not onebot['ok']:
        findings.append({'severity': 'critical', 'area': 'onebot', 'detail': f"OneBot unhealthy: {onebot.get('detail')}"})

    overall = 'READY' if not findings else 'NEEDS_ATTENTION'
    return {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'overall': overall,
        'units': units,
        'pending_inbox': pending,
        'active_inbox': active,
        'latest_audits': audits[:5],
        'non_pass_audits': non_pass,
        'listener_boundary': boundary,
        'onebot': onebot,
        'findings': findings,
    }


def render(data: dict) -> str:
    lines = [
        '# GenericAgent Supervisor Snapshot',
        '',
        f"- generated_at: `{data['generated_at']}`",
        f"- overall: `{data['overall']}`",
        f"- findings: `{len(data['findings'])}`",
        '',
        '## Findings',
        '',
    ]
    if data['findings']:
        for f in data['findings']:
            lines.append(f"- `{f['severity']}` **{f['area']}**: {f['detail']}")
    else:
        lines.append('- none')
    lines += ['', '## Services', '']
    for unit, state in data['units'].items():
        lines.append(f"- `{'OK' if state == 'active' else 'FAIL'}` {unit}: `{state}`")
    lines += [
        '',
        '## Inbox',
        '',
        f"- pending: `{len(data['pending_inbox'])}`",
        f"- active: `{len(data['active_inbox'])}`",
        '',
        '## OneBot',
        '',
        f"- ok: `{data['onebot']['ok']}`",
        f"- detail: `{data['onebot']['detail']}`",
        '',
        '## Listener Boundary',
        '',
        f"- ok: `{data['listener_boundary']['ok']}`",
        f"- public: `{data['listener_boundary']['public']}`",
        '```text',
        data['listener_boundary']['listeners'] or '(no matching listeners)',
        '```',
        '',
        '## Latest Audits',
        '',
    ]
    if data['latest_audits']:
        for audit in data['latest_audits']:
            lines.append(f"- `{audit.get('verdict', 'UNKNOWN')}` {audit.get('task', '?')} -> `{audit.get('_path')}`")
    else:
        lines.append('- none')
    lines += [
        '',
        '## Operator Commands',
        '',
        '```bash',
        'genericagent-assistant doctor --refresh',
        'genericagent-assistant supervise --refresh --tail 120',
        'genericagent-assistant alerts --verbose',
        'genericagent-assistant submit --now "你的任务"',
        '```',
        '',
    ]
    return '\n'.join(lines)


def write_outputs(data: dict) -> None:
    SUPERVISION_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    CURRENT_MD.write_text(render(data), encoding='utf-8')
    with HISTORY_JSONL.open('a', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n')
    if data['findings']:
        ALERT_MD.write_text(render(data), encoding='utf-8')
    elif ALERT_MD.exists():
        ALERT_MD.unlink()


def main() -> int:
    data = collect()
    write_outputs(data)
    print(CURRENT_MD)
    print(f"overall={data['overall']} findings={len(data['findings'])}")
    return 0 if data['overall'] == 'READY' else 1


if __name__ == '__main__':
    raise SystemExit(main())
