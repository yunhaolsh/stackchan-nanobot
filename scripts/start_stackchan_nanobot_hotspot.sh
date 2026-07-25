#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${STACKCHAN_ENV_FILE:-$ROOT_DIR/.run/stackchan-nanobot.env}"
INFERENCE_MODE_OVERRIDE="${STACKCHAN_INFERENCE_MODE:-}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -n "$INFERENCE_MODE_OVERRIDE" ]]; then
  export STACKCHAN_INFERENCE_MODE="$INFERENCE_MODE_OVERRIDE"
fi

export STACKCHAN_PUBLIC_HOST="${STACKCHAN_PUBLIC_HOST:-stackchan-nanobot.local}"
export STACKCHAN_RESTART_BRIDGE="${STACKCHAN_RESTART_BRIDGE:-1}"
export STACKCHAN_ENABLE_NANOBOT_API="${STACKCHAN_ENABLE_NANOBOT_API:-0}"
export STACKCHAN_INFERENCE_MODE="${STACKCHAN_INFERENCE_MODE:-cloud}"

if [[ "$STACKCHAN_INFERENCE_MODE" != "local" ]]; then
  export STACKCHAN_CHAT_PROVIDER="${STACKCHAN_CHAT_PROVIDER:-glm}"
  if [[ -z "${STACKCHAN_CHAT_MODEL:-}" ]]; then
    case "$STACKCHAN_CHAT_PROVIDER" in
      deepseek) export STACKCHAN_CHAT_MODEL="deepseek-v4-flash" ;;
      *) export STACKCHAN_CHAT_MODEL="glm-4.7-flash" ;;
    esac
  fi
  export STACKCHAN_ASR_PROVIDER="${STACKCHAN_ASR_PROVIDER:-glm}"
  export STACKCHAN_ASR_MODEL="${STACKCHAN_ASR_MODEL:-glm-asr-2512}"
  export STACKCHAN_TTS_PROVIDER="${STACKCHAN_TTS_PROVIDER:-glm}"
  export STACKCHAN_TTS_MODEL="${STACKCHAN_TTS_MODEL:-glm-tts}"
  export STACKCHAN_TTS_VOICE="${STACKCHAN_TTS_VOICE:-tongtong}"
  export STACKCHAN_VISION_PROVIDER="${STACKCHAN_VISION_PROVIDER:-glm}"
  export STACKCHAN_VISION_MODEL="${STACKCHAN_VISION_MODEL:-glm-4.6v-flash}"
fi

exec "$ROOT_DIR/scripts/start_stackchan_nanobot.sh"
