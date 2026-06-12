# ══════════════════════════════════════════════════════════════════════════════
#  GenericAgent — WSL / Git Bash environment setup ONLY
#  Windows (PowerShell) users: use env.ps1 instead or just run python directly.
#  This file is sourced by the ./ga bash launcher. On Windows, use ga.cmd.
# ══════════════════════════════════════════════════════════════════════════════
export PORTABLE_DEV_ROOT="/home/exusiai/GenericAgent/.portable"
export GENERICAGENT_HOME="/home/exusiai/GenericAgent"
export UV_PYTHON_INSTALL_DIR="/home/exusiai/GenericAgent/.portable/uv-python"
export UV_CACHE_DIR="/home/exusiai/GenericAgent/.portable/uv-cache"
export PATH="/home/exusiai/GenericAgent/.portable/bin:/home/exusiai/GenericAgent/.portable/uv-python/cpython-3.12.13-linux-x86_64-gnu/bin:$PATH"

# ── API Credentials (read by mykey.py) ─────────────────────────────────────
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY}"
export DEEPSEEK_API_BASE="${DEEPSEEK_API_BASE:-https://api.deepseek.com/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-v4-flash}"
export GENERICAGENT_PROXY="${GENERICAGENT_PROXY:-http://172.22.160.1:1080}"

echo "Activated GenericAgent portable env: $GENERICAGENT_HOME"
