#!/bin/bash
# GenericAgent Status Monitor
set -u

ROOT=/opt/GenericAgent
PY=$ROOT/venv/bin/python

ok_msg() { echo "[OK] $1"; }
warn_msg() { echo "[WARN] $1"; }
fail_msg() { echo "[FAIL] $1"; }

check_svc() {
    local label="$1" svc="$2"
    if systemctl is-active --quiet "$svc"; then ok_msg "$label"; else fail_msg "$label"; fi
}

ws_status() {
    cd "$ROOT" || return 1
    "$PY" - <<'PY'
import asyncio, json, time
import aiohttp
async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect('ws://127.0.0.1:3001/ws', timeout=8) as ws:
            echo='status_'+str(int(time.time()*1000))
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
try:
    raise SystemExit(asyncio.run(main()))
except Exception as e:
    print(f'{type(e).__name__}: {e}')
    raise SystemExit(1)
PY
}

echo '=============================='
echo ' GenericAgent Status Report'
echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo ''

check_svc 'Scheduler' 'genericagent'
check_svc 'NapCatQQ Frontend' 'genericagent-napcat'
check_svc 'QQ/NapCat Runtime' 'genericagent-qq'
check_svc 'Port Guard' 'genericagent-port-guard'
if systemctl is-active --quiet genericagent-inbox.timer; then ok_msg 'Inbox Timer'; else fail_msg 'Inbox Timer'; fi
if systemctl is-active --quiet genericagent-watchdog.timer; then ok_msg 'Watchdog Timer'; else fail_msg 'Watchdog Timer'; fi
if systemctl is-active --quiet genericagent-dashboard.timer; then ok_msg 'Dashboard Timer'; else fail_msg 'Dashboard Timer'; fi
if systemctl is-active --quiet genericagent-supervisor.timer; then ok_msg 'Supervisor Timer'; else fail_msg 'Supervisor Timer'; fi

pgrep -fa '/opt/QQ/qq' >/dev/null && ok_msg 'QQ Process' || fail_msg 'QQ Process'
ss -ltnp 2>/dev/null | grep -q '127\.0\.0\.1:3001' && ok_msg 'OneBot WS 127.0.0.1:3001' || fail_msg 'OneBot WS 127.0.0.1:3001'
ss -ltnp 2>/dev/null | grep -q '127\.0\.0\.1:6099' && ok_msg 'WebUI 127.0.0.1:6099' || warn_msg 'WebUI 127.0.0.1:6099'
if ss -ltnp 2>/dev/null | grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*):3001|(^|[[:space:]])(0\.0\.0\.0|\*):6099'; then
    fail_msg 'NapCat ports are publicly bound'
else
    ok_msg 'NapCat ports local-only'
fi
iptables -C INPUT ! -i lo -p tcp -m multiport --dports 3000,3001,6099 -j DROP 2>/dev/null && ok_msg 'Port guard iptables rule' || warn_msg 'Port guard iptables rule missing'

echo ''
echo '-- OneBot health --'
if out=$(ws_status 2>&1); then ok_msg "OneBot get_status $out"; else fail_msg "OneBot get_status $out"; fi

echo ''
echo '-- Dashboard --'
dashboard="$ROOT/sandbox/reports/assistant_dashboard.md"
if [ -f "$dashboard" ]; then
    echo "Assistant dashboard: $dashboard"
    grep -E '^- overall:|^- latest_inbox_supervision:' "$dashboard" 2>/dev/null || true
else
    echo "Assistant dashboard: missing"
fi
echo ''

echo '-- Supervisor --'
supervisor="$ROOT/sandbox/reports/supervision/current.md"
if [ -f "$supervisor" ]; then
    echo "Supervisor snapshot: $supervisor"
    grep -E '^- overall:|^- findings:' "$supervisor" 2>/dev/null || true
else
    echo "Supervisor snapshot: missing"
fi
echo ''

echo '-- Inbox --'
pending=$(find "$ROOT/sandbox/inbox" -maxdepth 1 -type f \( -name '*.md' -o -name '*.txt' -o -name '*.task' -o -name '*.json' \) 2>/dev/null | wc -l)
echo "Pending inbox files: $pending"
latest_report=$(ls -t "$ROOT"/sandbox/reports/inbox_results/*.md 2>/dev/null | head -1 || true)
if [ -n "$latest_report" ]; then
    echo "Latest inbox report: $latest_report"
fi
latest_audit_json=$(ls -t "$ROOT"/sandbox/reports/inbox_audits/*.json 2>/dev/null | head -1 || true)
if [ -n "$latest_audit_json" ]; then
    verdict=$("$PY" - <<PY "$latest_audit_json"
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8')).get('verdict', 'UNKNOWN'))
PY
)
    echo "Latest inbox supervision: $verdict ($latest_audit_json)"
fi
echo ''

echo '-- Logs --'
echo '[NapCat] last 3:'
tail -3 "$ROOT/frontends/temp/napcat_qqapp.log" 2>/dev/null | sed 's/^/  /'
echo '[Scheduler] last 3:'
journalctl -u genericagent --no-pager -n 3 2>/dev/null | tail -3 | sed 's/^/  /'

echo ''
free -h | awk '/^Mem:/ {print "Memory: "$3"/"$2" available=" $7}'
df -h / | awk 'NR==2 {print "Disk /: "$3"/"$2" used ("$5")"}'
uptime -p
echo '=============================='
