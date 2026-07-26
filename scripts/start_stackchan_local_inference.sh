#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$ROOT_DIR/.run"
SPEECH_PY="${STACKCHAN_LOCAL_SPEECH_PYTHON:-$ROOT_DIR/.venv-local-speech/bin/python}"
LLAMA_SERVER="${STACKCHAN_LLAMA_SERVER:-$ROOT_DIR/.local-runtime/llama.cpp/build-stackchan/bin/llama-server}"
LLAMA_PORT="${STACKCHAN_LOCAL_CHAT_PORT:-18080}"
SPEECH_PORT="${STACKCHAN_LOCAL_SPEECH_PORT:-18081}"
LLAMA_MODEL="${STACKCHAN_LLAMA_MODEL_PATH:-$ROOT_DIR/models/llm/Qwen3-4B-Q4_K_M.gguf}"
LLAMA_ALIAS="${STACKCHAN_LOCAL_CHAT_MODEL:-Qwen3-4B}"
LLAMA_GPU_LAYERS="${STACKCHAN_LLAMA_GPU_LAYERS:-0}"

mkdir -p "$PID_DIR"
STARTED_NAMES=()
STARTED_PIDS=()

cleanup_started() {
  local index
  for index in "${!STARTED_PIDS[@]}"; do
    if kill -0 "${STARTED_PIDS[$index]}" 2>/dev/null; then
      kill "${STARTED_PIDS[$index]}" 2>/dev/null || true
    fi
    rm -f "$PID_DIR/${STARTED_NAMES[$index]}.pid"
  done
}

trap cleanup_started ERR INT TERM

start_one() {
  local name="$1"
  local probe_url="$2"
  local pid_file="$PID_DIR/$name.pid"
  shift 2
  local code
  code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "$probe_url" 2>/dev/null || true)"
  if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
    echo "$name already healthy: $probe_url"
    return
  fi
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$name already running: pid=$(cat "$pid_file")"
    return
  fi
  echo "Starting $name..."
  nohup "$@" >"$PID_DIR/$name.log" 2>&1 &
  echo "$!" >"$pid_file"
  STARTED_NAMES+=("$name")
  STARTED_PIDS+=("$!")
  echo "$name pid=$!"
}

wait_healthy() {
  local name="$1"
  local url="$2"
  local pid_file="$3"
  local timeout_s="${4:-60}"
  local waited=0
  local code
  while (( waited < timeout_s )); do
    code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
      echo "$name ready: $url"
      return 0
    fi
    if [[ ! -f "$pid_file" ]] || ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
      echo "$name stopped before becoming healthy; inspect $PID_DIR/$name.log" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "$name did not become healthy within ${timeout_s}s; inspect $PID_DIR/$name.log" >&2
  return 1
}

if [[ ! -x "$SPEECH_PY" ]]; then
  echo "Missing local speech environment. Run ./scripts/setup_stackchan_local_speech.sh" >&2
  exit 1
fi
if [[ ! -x "$LLAMA_SERVER" ]]; then
  echo "Missing llama-server. Run ./scripts/setup_stackchan_local_llm.sh" >&2
  exit 1
fi
if [[ ! -f "$LLAMA_MODEL" ]]; then
  echo "Missing local LLM model: $LLAMA_MODEL" >&2
  echo "Run ./scripts/setup_stackchan_local_llm.sh to download and verify it." >&2
  exit 1
fi

start_one stackchan-local-speech \
  "http://127.0.0.1:$SPEECH_PORT/health" \
  "$SPEECH_PY" -u "$ROOT_DIR/local_inference/speech_service.py" \
  --host 127.0.0.1 --port "$SPEECH_PORT"

llama_args=(
  --host 127.0.0.1 --port "$LLAMA_PORT"
  -m "$LLAMA_MODEL" --alias "$LLAMA_ALIAS"
  -c "${STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS:-8192}"
  -n "${STACKCHAN_LOCAL_CHAT_MAX_TOKENS:-256}"
  -t "${STACKCHAN_LLAMA_THREADS:-12}"
  -np "${STACKCHAN_LLAMA_PARALLEL:-1}"
  --cache-ram "${STACKCHAN_LLAMA_PROMPT_CACHE_MB:-512}"
  --jinja --reasoning off --offline --no-ui
)
if [[ "$LLAMA_GPU_LAYERS" != "0" ]]; then
  llama_args+=(-ngl "$LLAMA_GPU_LAYERS")
fi

start_one stackchan-local-llm \
  "http://127.0.0.1:$LLAMA_PORT/health" \
  "$LLAMA_SERVER" "${llama_args[@]}"

wait_healthy \
  stackchan-local-speech \
  "http://127.0.0.1:$SPEECH_PORT/health" \
  "$PID_DIR/stackchan-local-speech.pid" \
  "${STACKCHAN_LOCAL_SPEECH_START_TIMEOUT_S:-120}"
wait_healthy \
  stackchan-local-llm \
  "http://127.0.0.1:$LLAMA_PORT/health" \
  "$PID_DIR/stackchan-local-llm.pid" \
  "${STACKCHAN_LOCAL_LLM_START_TIMEOUT_S:-900}"

trap - ERR INT TERM
