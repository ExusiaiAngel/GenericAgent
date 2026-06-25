#!/bin/bash
# Start QQ with xvfb for NapCatQQ
# Memory-optimized for headless bot operation
set -e

export DISPLAY=:99

# NapCat env - disable multiprocessing features
# Note: NODE_OPTIONS ignored in packaged Electron apps (confirmed by node_bindings.cc:438)
# export NODE_OPTIONS="--max-old-space-size=128"

# Clean up stale state
rm -f /tmp/.X99-lock
for pid in $(pgrep -f "Xvfb.*:99" 2>/dev/null); do
    kill "$pid" 2>/dev/null || true
done
sleep 1

# Minimal virtual display
Xvfb :99 -screen 0 640x480x8 -ac &
XVFB_PID=$!
sleep 2

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "ERROR: Xvfb failed to start on display :99" >&2
    exit 1
fi

exec /opt/QQ/qq \
    --no-sandbox \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-accelerated-2d-canvas \
    --disable-background-networking \
    --disable-default-apps \
    --disable-extensions \
    --disable-sync \
    --disable-translate \
    --disable-speech-api \
    --disable-features=TranslateUI,BlinkGenPropertyTrees \
    --js-flags="--max-old-space-size=96" \
    --renderer-process-limit=1 \
    -q 1908184846
