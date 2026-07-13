#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_ROOT/output/logs"
PID_FILE="$PROJECT_ROOT/output/trendradar-mcp.pid"

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [ -z "$UV_BIN" ]; then
  echo "uv was not found; install it or set UV_BIN" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
cd "$PROJECT_ROOT"

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "TrendRadar MCP already running with PID $old_pid"
    exit 0
  fi
fi

nohup "$UV_BIN" run python -m mcp_server.server --transport http --host 127.0.0.1 --port 3333 \
  > "$LOG_DIR/mcp.log" 2>&1 &

pid="$!"
echo "$pid" > "$PID_FILE"
echo "Started TrendRadar MCP with PID $pid"
