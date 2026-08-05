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

ensure_port_guard
ensure_svc genericagent 'Scheduler'
ensure_svc genericagent-tg 'Telegram frontend'

LOG_FILE=$ROOT/frontends/temp/tgapp.log
if [ -f "$LOG_FILE" ]; then
    CUR_TS=$(date +%s 2>/dev/null)
    FILE_TS=$(stat -c %Y "$LOG_FILE" 2>/dev/null)
    if [ -n "${CUR_TS:-}" ] && [ -n "${FILE_TS:-}" ]; then
        AGE=$((CUR_TS - FILE_TS))
        if [ "$AGE" -gt 3600 ]; then
            log "WARNING: tgapp log stale (${AGE}s)"
        fi
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
