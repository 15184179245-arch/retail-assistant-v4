#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo "需要安装 uv 和 Python 3.12"; exit 1; }
command -v node >/dev/null && command -v npm >/dev/null || { echo "需要安装 Node.js 24 和 npm"; exit 1; }
node -e 'if (+process.versions.node.split(".")[0] < 24) { console.error("需要 Node.js 24 或更高版本"); process.exit(1); }'
uv sync --locked --python 3.12
(cd apps/web && npm ci --ignore-scripts --no-audit --no-fund && npm run build)
DEMO_PORT="${DEMO_PORT:-8900}"
# Drop all incoming settings so vendor credentials and database URLs cannot
# leak into the demo app even from an already configured developer shell.
exec env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m backend.demo "$DEMO_PORT"
