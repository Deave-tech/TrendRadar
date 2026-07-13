#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$PROJECT_ROOT/output/trendradar-mcp.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "No PID file found"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "$pid" ]; then
  rm -f "$PID_FILE"
  echo "Empty PID file removed"
  exit 0
fi

if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "Stopped TrendRadar MCP PID $pid"
else
  echo "PID $pid is not running"
fi

rm -f "$PID_FILE"
