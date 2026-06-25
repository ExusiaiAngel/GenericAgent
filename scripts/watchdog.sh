#!/bin/bash
# GenericAgent self-healing watchdog. Safe to run repeatedly from systemd timer.
set -u
ROOT=/opt/GenericAgent
PY=$ROOT/venv/bin/python
LOG=$ROOT/frontends/temp/watchdog.log
mkdir -p "$(dirname "$LOG")"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg" >> "$LOG"
    echo "$msg" >&2
}

ensure_svc() {
    local svc="$1" label="$2"
    if ! systemctl is-active --quiet "$svc"; then
        log "$label DOWN, restarting..."
        systemctl restart "$svc" >>"$LOG" 2>&1
        sleep 3
        systemctl is-active --quiet "$svc" && log "$label recovered" || log "$label FAILED to restart"
    fi
}

ensure_port_guard() {
    if ! iptables -C INPUT ! -i lo -p tcp -m multiport --dports 3000,3001,6099 -j DROP 2>/dev/null; then
        log 'Port guard rule missing, restoring...'
        iptables -I INPUT 1 ! -i lo -p tcp -m multiport --dports 3000,3001,6099 -j DROP >>"$LOG" 2>&1 || log 'Port guard restore failed'
    fi
}

onebot_ok() {
    cd "$ROOT" || return 1
    "$PY" - <<'PY' >/dev/null 2>&1
import asyncio, json, time
import aiohttp
async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect('ws://127.0.0.1:3001/ws', timeout=8) as ws:
            echo='watchdog_'+str(int(time.time()*1000))
            await ws.send_json({'action':'get_status','params':{},'echo':echo})
            deadline=time.time()+8
            while time.time()<deadline:
                msg=await ws.receive(timeout=max(0.1, deadline-time.time()))
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data=json.loads(msg.data)
                    if data.get('echo') == echo:
                        d=data.get('data') or {}
                        raise SystemExit(0 if d.get('online') and d.get('good') else 2)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    raise SystemExit(3)
            raise SystemExit(4)
asyncio.run(main())
PY
}

ensure_port_guard
ensure_svc genericagent 'Scheduler'
ensure_svc genericagent-qq 'QQ/NapCat runtime'
ensure_svc genericagent-napcat 'NapCat frontend'

if ! onebot_ok; then
    log 'OneBot get_status failed, restarting QQ runtime and NapCat frontend...'
    systemctl restart genericagent-qq >>"$LOG" 2>&1
    sleep 10
    systemctl restart genericagent-napcat >>"$LOG" 2>&1
    sleep 5
    if onebot_ok; then log 'OneBot recovered'; else log 'OneBot still unhealthy after restart'; fi
fi

if ss -ltnp 2>/dev/null | grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*):3001|(^|[[:space:]])(0\.0\.0\.0|\*):6099'; then
    log 'SECURITY: NapCat port bound publicly; restarting QQ runtime to reload local-only config'
    systemctl restart genericagent-qq >>"$LOG" 2>&1
fi

LOG_FILE=$ROOT/frontends/temp/napcat_qqapp.log
if [ -f "$LOG_FILE" ]; then
    CUR_TS=$(date +%s 2>/dev/null)
    FILE_TS=$(stat -c %Y "$LOG_FILE" 2>/dev/null)
    if [ -n "${CUR_TS:-}" ] && [ -n "${FILE_TS:-}" ]; then
        AGE=$((CUR_TS - FILE_TS))
        if [ "$AGE" -gt 600 ]; then
            log "WARNING: napcat log stale (${AGE}s)"
        fi
    fi
fi

# ── Daily QQ restart at 4 AM to free memory ──
HOUR=$(date +%H)
if [ "$HOUR" = "04" ]; then
    MIN=$(date +%M)
    if [ "$MIN" -lt 10 ]; then  # restart once within 04:00-04:09 window
        log "Daily QQ restart (scheduled 4am)"
        systemctl restart genericagent-qq >>"$LOG" 2>&1
        sleep 10
        systemctl restart genericagent-napcat >>"$LOG" 2>&1
        log "Daily restart complete"
    fi
fi

# ── Housekeeping: clean stale sub-agent dirs (>24h) ──
CUTOFF=$(date -d '24 hours ago' +%s 2>/dev/null || echo 0)
SUB_DIR=$ROOT/temp/sub_agents
if [ -d "$SUB_DIR" ] && [ "$CUTOFF" -gt 0 ]; then
    for d in "$SUB_DIR"/sub_*/; do
        [ -d "$d" ] || continue
        DIR_TS=$(stat -c %Y "$d" 2>/dev/null || echo 0)
        if [ "$DIR_TS" -gt 0 ] && [ "$DIR_TS" -lt "$CUTOFF" ]; then
            rm -rf "$d"
            log "cleaned stale sub-agent: $(basename "$d")"
        fi
    done
fi

# ── Housekeeping: clean jmcomic cache (>7 days) ──
CUTOFF_7D=$(date -d '7 days ago' +%s 2>/dev/null || echo 0)
JM_DIR=$ROOT/temp/jmcomic
if [ -d "$JM_DIR" ] && [ "$CUTOFF_7D" -gt 0 ]; then
    for d in "$JM_DIR"/*/; do
        [ -d "$d" ] || continue
        DIR_TS=$(stat -c %Y "$d" 2>/dev/null || echo 0)
        if [ "$DIR_TS" -gt 0 ] && [ "$DIR_TS" -lt "$CUTOFF_7D" ]; then
            rm -rf "$d"
            log "cleaned old jmcomic cache: $(basename "$d")"
        fi
    done
fi

# ── Housekeeping: clean old model_responses (>7 days) ──
MR_DIR=$ROOT/temp/model_responses
if [ -d "$MR_DIR" ] && [ "$CUTOFF_7D" -gt 0 ]; then
    find "$MR_DIR" -name 'model_responses_*.txt' -mtime +7 -delete 2>/dev/null
fi
