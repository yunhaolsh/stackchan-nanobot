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

CHAT_PROVIDER="${STACKCHAN_CHAT_PROVIDER:-glm}"
case "$CHAT_PROVIDER" in
  glm)
    export OPENAI_API_KEY="${STACKCHAN_CHAT_API_KEY:-${ZHIPU_API_KEY:-${GLM_API_KEY:-}}}"
    export OPENAI_BASE_URL="${STACKCHAN_GLM_BASE_URL:-https://open.bigmodel.cn/api/paas/v4}"
    export STACKCHAN_CHAT_MODEL="${STACKCHAN_CHAT_MODEL:-glm-4.7-flash}"
    ;;
  deepseek)
    export OPENAI_API_KEY="${STACKCHAN_CHAT_API_KEY:-${DEEPSEEK_API_KEY:-}}"
    export OPENAI_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com}"
    export STACKCHAN_CHAT_MODEL="${STACKCHAN_CHAT_MODEL:-deepseek-v4-flash}"
    ;;
  openai-compatible)
    : "${OPENAI_API_KEY:?OPENAI_API_KEY is required for STACKCHAN_CHAT_PROVIDER=openai-compatible}"
    : "${OPENAI_BASE_URL:?OPENAI_BASE_URL is required for STACKCHAN_CHAT_PROVIDER=openai-compatible}"
    ;;
  *)
    echo "Unsupported STACKCHAN_CHAT_PROVIDER: $CHAT_PROVIDER" >&2
    exit 1
    ;;
esac

if [[ -z "$OPENAI_API_KEY" ]]; then
  echo "Missing API key for chat provider '$CHAT_PROVIDER'. Set it in the local env file." >&2
  exit 1
fi

# Nanobot's OpenAI-compatible provider uses this variable for the real HTTP
# timeout. Keep the StackChan-facing name as the portable configuration API.
export NANOBOT_OPENAI_COMPAT_TIMEOUT_S="${NANOBOT_OPENAI_COMPAT_TIMEOUT_S:-${STACKCHAN_CHAT_TIMEOUT:-20}}"

if [[ "$CHAT_PROVIDER" == "glm" && "$STACKCHAN_CHAT_MODEL" != glm-* ]]; then
  echo "STACKCHAN_CHAT_PROVIDER=glm requires a glm-* chat model, got: $STACKCHAN_CHAT_MODEL" >&2
  exit 1
fi
if [[ "$CHAT_PROVIDER" == "glm" && "${STACKCHAN_CHAT_THINKING:-disabled}" != "disabled" && "${STACKCHAN_CHAT_THINKING:-disabled}" != "enabled" ]]; then
  echo "STACKCHAN_CHAT_THINKING must be 'disabled' or 'enabled'." >&2
  exit 1
fi
if [[ "$CHAT_PROVIDER" == "deepseek" && "$STACKCHAN_CHAT_MODEL" != deepseek-* ]]; then
  echo "STACKCHAN_CHAT_PROVIDER=deepseek requires a deepseek-* chat model, got: $STACKCHAN_CHAT_MODEL" >&2
  exit 1
fi

append_no_proxy_host() {
  local url="$1"
  local host="${url#*://}"
  host="${host%%/*}"
  host="${host%%:*}"
  if [[ -z "$host" ]]; then
    return
  fi
  case ",${NO_PROXY:-}," in
    *,"$host",*) ;;
    *) export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$host" ;;
  esac
  case ",${no_proxy:-}," in
    *,"$host",*) ;;
    *) export no_proxy="${no_proxy:+$no_proxy,}$host" ;;
  esac
}

if [[ "${STACKCHAN_BYPASS_PROVIDER_PROXY:-0}" == "1" ]]; then
  append_no_proxy_host "$OPENAI_BASE_URL"
  append_no_proxy_host "${STACKCHAN_GLM_BASE_URL:-https://open.bigmodel.cn/api/paas/v4}"
fi

ZHIPU_KEY="${ZHIPU_API_KEY:-${GLM_API_KEY:-}}"
if [[ "${STACKCHAN_ASR_PROVIDER:-glm}" == "glm" ]]; then
  if [[ -z "${STACKCHAN_ASR_API_KEY:-$ZHIPU_KEY}" ]]; then
    echo "GLM ASR requires ZHIPU_API_KEY, GLM_API_KEY, or STACKCHAN_ASR_API_KEY." >&2
    exit 1
  fi
  if [[ "${STACKCHAN_ASR_MODEL:-glm-asr-2512}" != glm-asr-* ]]; then
    echo "STACKCHAN_ASR_PROVIDER=glm requires a glm-asr-* model." >&2
    exit 1
  fi
fi
if [[ "${STACKCHAN_TTS_PROVIDER:-glm}" == "glm" ]]; then
  if [[ -z "${STACKCHAN_TTS_API_KEY:-$ZHIPU_KEY}" ]]; then
    echo "GLM TTS requires ZHIPU_API_KEY, GLM_API_KEY, or STACKCHAN_TTS_API_KEY." >&2
    exit 1
  fi
  if [[ "${STACKCHAN_TTS_MODEL:-glm-tts}" != glm-tts* ]]; then
    echo "STACKCHAN_TTS_PROVIDER=glm requires a glm-tts model." >&2
    exit 1
  fi
fi
if [[ "${STACKCHAN_VISION_PROVIDER:-glm}" == "glm" ]]; then
  if [[ -z "${STACKCHAN_VISION_API_KEY:-$ZHIPU_KEY}" ]]; then
    echo "GLM Vision requires ZHIPU_API_KEY, GLM_API_KEY, or STACKCHAN_VISION_API_KEY." >&2
    exit 1
  fi
  if [[ "${STACKCHAN_VISION_MODEL:-glm-4.6v-flash}" != glm-*v-* ]]; then
    echo "STACKCHAN_VISION_PROVIDER=glm requires a GLM vision model such as glm-4.6v-flash." >&2
    exit 1
  fi
fi

if [[ -z "${STACKCHAN_ASR_COMMAND:-}" ]]; then
  if [[ "${STACKCHAN_ASR_PROVIDER:-glm}" == "glm" ]]; then
    export STACKCHAN_ASR_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_asr_glm.py"
  elif [[ "${STACKCHAN_ASR_PROVIDER:-}" == "gemini" ]]; then
    export STACKCHAN_ASR_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_asr_gemini.py"
  elif [[ "${STACKCHAN_ENABLE_OPENAI_ASR:-0}" == "1" || "${STACKCHAN_ASR_PROVIDER:-}" == "openai" ]]; then
    export STACKCHAN_ASR_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_asr_openai.py"
  fi
fi

if [[ -z "${STACKCHAN_TTS_COMMAND:-}" ]]; then
  if [[ "${STACKCHAN_TTS_PROVIDER:-glm}" == "glm" ]]; then
    export STACKCHAN_TTS_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_tts_glm.py"
    export STACKCHAN_TTS_STREAMING="${STACKCHAN_TTS_STREAMING:-1}"
    export STACKCHAN_TTS_COMMAND_STREAMING="${STACKCHAN_TTS_COMMAND_STREAMING:-1}"
  elif [[ "${STACKCHAN_TTS_PROVIDER:-}" == "gemini" ]]; then
    export STACKCHAN_TTS_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_tts_gemini.py"
  elif [[ "${STACKCHAN_ENABLE_OPENAI_TTS:-0}" == "1" || "${STACKCHAN_TTS_PROVIDER:-}" == "openai" ]]; then
    export STACKCHAN_TTS_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_tts_openai.py"
  fi
fi

if [[ -z "${STACKCHAN_VISION_COMMAND:-}" && "${STACKCHAN_VISION_PROVIDER:-glm}" == "glm" ]]; then
  export STACKCHAN_VISION_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_vision_glm.py"
fi

detect_public_host() {
  ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}'
}

LAN_IP="${STACKCHAN_MDNS_ADDRESS:-$(detect_public_host || true)}"
PUBLIC_HOST="${STACKCHAN_PUBLIC_HOST:-$LAN_IP}"

mkdir -p "$PID_DIR" "$ROOT_DIR/nanobot_config/workspace"

if [[ -n "${STACKCHAN_CHAT_MODEL:-}" ]]; then
  RUNTIME_NANOBOT_CONFIG="$PID_DIR/nanobot_config.runtime.json"
  "$VENV_PY" - \
    "$NANOBOT_CONFIG" \
    "$RUNTIME_NANOBOT_CONFIG" \
    "$STACKCHAN_CHAT_MODEL" \
    "$CHAT_PROVIDER" \
    "${STACKCHAN_CHAT_THINKING:-disabled}" <<'PY'
import json
import sys

src, dst, model, provider_name, thinking = sys.argv[1:6]
with open(src, "r", encoding="utf-8") as f:
    config = json.load(f)
config.setdefault("modelPresets", {}).setdefault("primary", {})["model"] = model
provider = config.setdefault("providers", {}).setdefault("openai", {})
if provider_name in {"glm", "deepseek"}:
    provider["extraBody"] = {"thinking": {"type": thinking}}
else:
    provider.pop("extraBody", None)
with open(dst, "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  NANOBOT_CONFIG="$RUNTIME_NANOBOT_CONFIG"
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
if [[ "$PUBLIC_HOST" == *.local ]]; then
  echo "mDNS alias      : $PUBLIC_HOST -> $LAN_IP"
fi
