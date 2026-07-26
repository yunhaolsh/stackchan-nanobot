#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_PORT="${STACKCHAN_BRIDGE_PORT:-12800}"
NANOBOT_API_PORT="${NANOBOT_API_PORT:-8900}"
NANOBOT_CONFIG="${NANOBOT_CONFIG:-$ROOT_DIR/nanobot_config/config.json}"
MDNS_HOST="${STACKCHAN_MDNS_HOST:-stackchan-nanobot.local}"
VENV_PY="$ROOT_DIR/.venv-nanobot/bin/python"
NANOBOT_BIN="$ROOT_DIR/.venv-nanobot/bin/nanobot"
PID_DIR="$ROOT_DIR/.run"
export NANOBOT_WORKSPACE="${NANOBOT_WORKSPACE:-$ROOT_DIR/nanobot_config/workspace}"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/stackchan_inference_env.sh"

detect_public_host() {
  ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}'
}

LAN_IP="${STACKCHAN_MDNS_ADDRESS:-$(detect_public_host || true)}"
PUBLIC_HOST="${STACKCHAN_PUBLIC_HOST:-$LAN_IP}"

mkdir -p "$PID_DIR" "$ROOT_DIR/nanobot_config/workspace"

if [[ -n "${STACKCHAN_CHAT_MODEL:-}" ]]; then
  RUNTIME_NANOBOT_CONFIG="$PID_DIR/nanobot_config.runtime.json"
  "$VENV_PY" "$ROOT_DIR/scripts/build_nanobot_runtime_config.py" \
    "$NANOBOT_CONFIG" \
    "$RUNTIME_NANOBOT_CONFIG" \
    --mode "$INFERENCE_MODE" \
    --chat-model "$STACKCHAN_CHAT_MODEL" \
    --chat-provider "$CHAT_PROVIDER" \
    --thinking "${STACKCHAN_CHAT_THINKING:-disabled}" \
    --local-model "${STACKCHAN_LOCAL_CHAT_MODEL:-Qwen3-4B}" \
    --local-context-tokens "${STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS:-16384}" \
    --local-max-tokens "${STACKCHAN_LOCAL_CHAT_MAX_TOKENS:-1024}" \
    --local-max-messages "${STACKCHAN_LOCAL_MAX_MESSAGES:-20}"
  NANOBOT_CONFIG="$RUNTIME_NANOBOT_CONFIG"
fi

if [[ "${STACKCHAN_CONFIG_ONLY:-0}" == "1" ]]; then
  echo "Inference mode   : $INFERENCE_MODE"
  echo "Chat provider    : $CHAT_PROVIDER"
  echo "Chat model       : $STACKCHAN_CHAT_MODEL"
  echo "Nanobot config   : $NANOBOT_CONFIG"
  if [[ "$INFERENCE_MODE" != "cloud" ]]; then
    echo "Local chat       : $STACKCHAN_LOCAL_CHAT_BASE_URL"
    echo "Local ASR        : $STACKCHAN_LOCAL_ASR_BASE_URL"
    echo "Local TTS        : $STACKCHAN_LOCAL_TTS_BASE_URL"
    echo "Local Vision     : $STACKCHAN_LOCAL_VISION_BASE_URL"
  fi
  exit 0
fi

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "Cannot detect LAN IP. Set STACKCHAN_PUBLIC_HOST manually or connect to a network." >&2
  exit 1
fi

if [[ "$PUBLIC_HOST" == *.local && -z "$LAN_IP" ]]; then
  echo "Cannot publish mDNS alias without a detected LAN IP. Use a direct IP for STACKCHAN_PUBLIC_HOST." >&2
  exit 1
fi

if [[ ! -x "$VENV_PY" || ! -x "$NANOBOT_BIN" ]]; then
  echo "Missing Nanobot virtualenv. Expected $VENV_PY and $NANOBOT_BIN" >&2
  exit 1
fi

if [[ ! -f "$NANOBOT_CONFIG" ]]; then
  echo "Missing Nanobot config: $NANOBOT_CONFIG" >&2
  exit 1
fi

start_if_needed() {
  local name="$1"
  local pid_file="$2"
  local probe_url="$3"
  shift 3

  if [[ -n "$probe_url" ]]; then
    local http_code
    http_code="$(curl -s -o /dev/null -w '%{http_code}' "$probe_url" 2>/dev/null || true)"
    if [[ "$http_code" != "000" ]]; then
      echo "$name already responding: $probe_url (http $http_code)"
      return
    fi
  fi

  if [[ -f "$pid_file" ]]; then
    local old_pid
    old_pid="$(cat "$pid_file")"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "$name already running: pid=$old_pid"
      return
    fi
  fi

  echo "Starting $name..."
  nohup "$@" >"$PID_DIR/$name.log" 2>&1 &
  echo "$!" >"$pid_file"
  echo "$name pid=$!"
}

wait_http() {
  local name="$1"
  local url="$2"
  local attempts="${3:-30}"
  local delay="${4:-0.5}"
  local http_code
  for _ in $(seq 1 "$attempts"); do
    http_code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$http_code" != "000" ]]; then
      return 0
    fi
    sleep "$delay"
  done
  echo "$name did not respond at $url after $attempts attempts" >&2
  return 1
}

local_health_url() {
  local base="${1%/}"
  base="${base%/v1}"
  printf '%s/health\n' "$base"
}

probe_local_inference() {
  if [[ "$INFERENCE_MODE" == "cloud" || "${STACKCHAN_LOCAL_HEALTHCHECK:-1}" != "1" ]]; then
    return 0
  fi

  local attempts="${STACKCHAN_LOCAL_HEALTH_ATTEMPTS:-3}"
  local delay="${STACKCHAN_LOCAL_HEALTH_DELAY_S:-1}"
  local failed=0
  local code
  local label
  local url
  local -A checked=()
  while read -r label url; do
    if [[ -n "${checked[$url]:-}" ]]; then
      continue
    fi
    checked[$url]=1
    code=000
    for _ in $(seq 1 "$attempts"); do
      code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
      if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
        break
      fi
      sleep "$delay"
    done
    if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
      echo "Local $label is not healthy at $url (http $code)" >&2
      failed=1
    fi
  done <<EOF
Chat $(local_health_url "$STACKCHAN_LOCAL_CHAT_BASE_URL")
ASR $(local_health_url "$STACKCHAN_LOCAL_ASR_BASE_URL")
TTS $(local_health_url "$STACKCHAN_LOCAL_TTS_BASE_URL")
EOF

  if [[ "${STACKCHAN_LOCAL_VISION_ENABLED:-0}" == "1" ]]; then
    url="$(local_health_url "$STACKCHAN_LOCAL_VISION_BASE_URL")"
    code=000
    for _ in $(seq 1 "$attempts"); do
      code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
      if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
        break
      fi
      sleep "$delay"
    done
    if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
      echo "Local Vision is not healthy at $url (http $code)" >&2
      failed=1
    fi
  fi

  if [[ "$failed" == "1" && "$INFERENCE_MODE" == "local" ]]; then
    echo "Pure local mode requires all enabled local inference services." >&2
    return 1
  fi
  if [[ "$failed" == "1" ]]; then
    echo "Hybrid mode will start, but unavailable local services cannot be used for fallback." >&2
  fi
}

stop_pid_file() {
  local name="$1"
  local pid_file="$2"
  if [[ ! -f "$pid_file" ]]; then
    return
  fi
  local old_pid
  old_pid="$(cat "$pid_file")"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Stopping stale $name pid=$old_pid"
    kill "$old_pid" || true
    sleep 1
    if kill -0 "$old_pid" 2>/dev/null; then
      echo "Force stopping stale $name pid=$old_pid"
      kill -KILL "$old_pid" || true
      sleep 1
    fi
  fi
  rm -f "$pid_file"
}

stop_bridge_on_port() {
  stop_listener_on_port "stackchan-bridge" "$BRIDGE_PORT"
}

stop_nanobot_on_port() {
  stop_listener_on_port "nanobot-api" "$NANOBOT_API_PORT"
}

stop_listener_on_port() {
  local name="$1"
  local port="$2"
  local stale_pids
  stale_pids="$(ss -ltnp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | sort -u)"
  if [[ -z "$stale_pids" ]]; then
    return
  fi
  echo "$stale_pids" | while read -r pid; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Stopping stale $name on port $port pid=$pid"
      kill "$pid" || true
      sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        echo "Force stopping stale $name on port $port pid=$pid"
        kill -KILL "$pid" || true
      fi
    fi
  done
  sleep 1
}

probe_local_inference

if [[ "${STACKCHAN_RESTART_NANOBOT:-0}" == "1" ]]; then
  stop_pid_file nanobot-api "$PID_DIR/nanobot-api.pid"
  stop_nanobot_on_port
fi

if [[ "${STACKCHAN_ENABLE_NANOBOT_API:-0}" == "1" ]]; then
  start_if_needed nanobot-api "$PID_DIR/nanobot-api.pid" \
    "http://127.0.0.1:$NANOBOT_API_PORT/v1/chat/completions" \
    "$NANOBOT_BIN" serve \
      --config "$NANOBOT_CONFIG" \
      --host 127.0.0.1 \
      --port "$NANOBOT_API_PORT"
else
  stop_pid_file nanobot-api "$PID_DIR/nanobot-api.pid"
fi

if [[ "$PUBLIC_HOST" == *.local ]]; then
  start_if_needed mdns-alias "$PID_DIR/mdns-alias.pid" "" \
    "$VENV_PY" -u "$ROOT_DIR/scripts/mdns_alias.py" \
      --name "$PUBLIC_HOST" \
      --address "$LAN_IP"
fi

if [[ "${STACKCHAN_RESTART_BRIDGE:-0}" == "1" ]]; then
  stop_pid_file stackchan-bridge "$PID_DIR/stackchan-bridge.pid"
  stop_bridge_on_port
fi

expected_ws_url="ws://$PUBLIC_HOST:$BRIDGE_PORT/ws"
current_ws_url="$(curl -s "http://127.0.0.1:$BRIDGE_PORT/health" 2>/dev/null \
  | "$VENV_PY" -c 'import json,sys; data=sys.stdin.read().strip(); print(json.loads(data).get("ws_url","") if data else "")' 2>/dev/null || true)"
if [[ -n "$current_ws_url" && "$current_ws_url" != "$expected_ws_url" ]]; then
  echo "stackchan-bridge has stale ws_url: $current_ws_url"
  echo "expected ws_url: $expected_ws_url"
  stop_pid_file stackchan-bridge "$PID_DIR/stackchan-bridge.pid"
  stop_bridge_on_port
fi

start_if_needed stackchan-bridge "$PID_DIR/stackchan-bridge.pid" \
  "http://127.0.0.1:$BRIDGE_PORT/health" \
  "$VENV_PY" -u "$ROOT_DIR/nanobot_bridge/server.py" \
    --host 0.0.0.0 \
    --port "$BRIDGE_PORT" \
    --public-host "$PUBLIC_HOST" \
    --nanobot-config "$NANOBOT_CONFIG"

wait_http stackchan-bridge "http://127.0.0.1:$BRIDGE_PORT/health" 30 0.5 || true

echo
echo "Bridge health:"
curl -s "http://127.0.0.1:$BRIDGE_PORT/health" || true
echo
echo
echo "StackChan OTA URL: http://$PUBLIC_HOST:$BRIDGE_PORT/xiaozhi/ota/"
echo "StackChan WS URL : ws://$PUBLIC_HOST:$BRIDGE_PORT/ws"
echo "Local MCP URL    : http://127.0.0.1:${STACKCHAN_MCP_PORT:-12801}/mcp"
echo "Nanobot backend  : embedded in stackchan-bridge"
echo "Inference mode   : $INFERENCE_MODE"
if [[ "${STACKCHAN_ENABLE_NANOBOT_API:-0}" == "1" ]]; then
  echo "Nanobot debug API: http://127.0.0.1:$NANOBOT_API_PORT/v1/chat/completions"
fi
echo "Chat provider    : $CHAT_PROVIDER"
if [[ -n "${STACKCHAN_CHAT_MODEL:-}" ]]; then
  echo "Chat model       : $STACKCHAN_CHAT_MODEL"
else
  echo "Chat model       : from $NANOBOT_CONFIG"
fi
if [[ -n "${STACKCHAN_ASR_COMMAND:-}" ]]; then
  echo "ASR command      : $STACKCHAN_ASR_COMMAND"
  if [[ "${STACKCHAN_ASR_PROVIDER:-}" == "glm" ]]; then
    echo "ASR provider     : glm (${STACKCHAN_ASR_MODEL:-glm-asr-2512})"
  elif [[ "${STACKCHAN_ASR_PROVIDER:-}" == "gemini" ]]; then
    echo "ASR provider     : gemini"
  elif [[ -z "${STACKCHAN_ASR_BASE_URL:-}" && -n "${OPENAI_BASE_URL:-}" ]]; then
    echo "ASR warning      : using OPENAI_BASE_URL fallback; set STACKCHAN_ASR_BASE_URL if this endpoint lacks /audio/transcriptions"
  fi
else
  echo "ASR command      : not configured"
fi
if [[ -n "${STACKCHAN_TTS_COMMAND:-}" ]]; then
  echo "TTS command      : $STACKCHAN_TTS_COMMAND"
  if [[ "${STACKCHAN_TTS_PROVIDER:-}" == "glm" ]]; then
    echo "TTS provider     : glm (${STACKCHAN_TTS_MODEL:-glm-tts}/${STACKCHAN_TTS_VOICE:-tongtong})"
  elif [[ "${STACKCHAN_TTS_PROVIDER:-}" == "gemini" ]]; then
    echo "TTS provider     : gemini"
  elif [[ -z "${STACKCHAN_TTS_BASE_URL:-}" && -n "${OPENAI_BASE_URL:-}" ]]; then
    echo "TTS warning      : using OPENAI_BASE_URL fallback; set STACKCHAN_TTS_BASE_URL if this endpoint lacks /audio/speech"
  fi
else
  echo "TTS command      : not configured"
fi
if [[ -n "${STACKCHAN_VISION_COMMAND:-}" ]]; then
  echo "Vision command   : $STACKCHAN_VISION_COMMAND"
else
  echo "Vision command   : not configured"
fi
if [[ "$INFERENCE_MODE" == "hybrid" ]]; then
  echo "Local fallback   : ${STACKCHAN_LOCAL_CHAT_MODEL} @ ${STACKCHAN_LOCAL_CHAT_BASE_URL}"
fi
if [[ "$PUBLIC_HOST" == *.local ]]; then
  echo "mDNS alias      : $PUBLIC_HOST -> $LAN_IP"
fi
