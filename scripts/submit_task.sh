#!/bin/bash
# Submit a task to GenericAgent inbox.
set -euo pipefail
INBOX=/opt/GenericAgent/sandbox/inbox
mkdir -p "$INBOX"
if [ "$#" -eq 0 ]; then
    echo "Usage: $0 'task text' OR $0 --file /path/to/task.md" >&2
    exit 2
fi
if [ "${1:-}" = "--file" ]; then
    src="${2:-}"
    [ -f "$src" ] || { echo "file not found: $src" >&2; exit 2; }
    base=$(basename "$src")
    dest="$INBOX/$(date +%Y%m%d_%H%M%S)_$base"
    cp "$src" "$dest"
else
    dest="$INBOX/$(date +%Y%m%d_%H%M%S)_manual.md"
    printf '%s
' "$*" > "$dest"
fi
echo "$dest"
