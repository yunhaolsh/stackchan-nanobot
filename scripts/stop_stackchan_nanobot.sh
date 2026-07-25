#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$ROOT_DIR/.run"
BRIDGE_PORT="${STACKCHAN_BRIDGE_PORT:-12800}"
NANOBOT_API_PORT="${NANOBOT_API_PORT:-8900}"
MCP_PORT="${STACKCHAN_MCP_PORT:-12801}"

stop_one() {
  local name="$1"
  local pid_file="$PID_DIR/$name.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "$name not tracked"
    return
  fi

  local pid
  pid="$(cat "$pid_file")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Stopping $name pid=$pid"
    kill "$pid" || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      echo "Force stopping $name pid=$pid"
      kill -KILL "$pid" || true
    fi
  else
    echo "$name pid not running"
  fi
  rm -f "$pid_file"
}

stop_one stackchan-bridge
stop_one mdns-alias
stop_one nanobot-api

stop_port() {
  local name="$1"
  local port="$2"
  local stale_pids
  stale_pids="$(ss -ltnp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | sort -u)"
  if [[ -z "$stale_pids" ]]; then
    return
  fi
  echo "$stale_pids" | while read -r pid; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Stopping untracked $name on port $port pid=$pid"
      kill "$pid" || true
      sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        echo "Force stopping untracked $name on port $port pid=$pid"
        kill -KILL "$pid" || true
      fi
    fi
  done
}

stop_port stackchan-bridge "$BRIDGE_PORT"
stop_port stackchan-mcp "$MCP_PORT"
stop_port nanobot-api "$NANOBOT_API_PORT"
