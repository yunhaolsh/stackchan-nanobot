#!/usr/bin/env bash
# Resolve cloud/local/hybrid provider settings. This file is sourced by the
# main launcher after ROOT_DIR, VENV_PY, and NANOBOT_CONFIG are defined.

: "${ROOT_DIR:?ROOT_DIR is required}"
: "${VENV_PY:?VENV_PY is required}"

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

configure_cloud_chat() {
  CHAT_PROVIDER="${STACKCHAN_CLOUD_CHAT_PROVIDER:-${STACKCHAN_CHAT_PROVIDER:-glm}}"
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
      echo "Unsupported cloud chat provider: $CHAT_PROVIDER" >&2
      return 1
      ;;
  esac

  if [[ -z "$OPENAI_API_KEY" ]]; then
    echo "Missing API key for cloud chat provider '$CHAT_PROVIDER'." >&2
    return 1
  fi
  if [[ "$CHAT_PROVIDER" == "glm" && "$STACKCHAN_CHAT_MODEL" != glm-* ]]; then
    echo "GLM chat requires a glm-* model, got: $STACKCHAN_CHAT_MODEL" >&2
    return 1
  fi
  if [[ "$CHAT_PROVIDER" == "deepseek" && "$STACKCHAN_CHAT_MODEL" != deepseek-* ]]; then
    echo "DeepSeek chat requires a deepseek-* model, got: $STACKCHAN_CHAT_MODEL" >&2
    return 1
  fi
  if [[ "${STACKCHAN_CHAT_THINKING:-disabled}" != "disabled" && "${STACKCHAN_CHAT_THINKING:-disabled}" != "enabled" ]]; then
    echo "STACKCHAN_CHAT_THINKING must be 'disabled' or 'enabled'." >&2
    return 1
  fi
}

configure_cloud_media() {
  local zhipu_key="${ZHIPU_API_KEY:-${GLM_API_KEY:-}}"

  export STACKCHAN_ASR_PROVIDER="${STACKCHAN_ASR_PROVIDER:-glm}"
  export STACKCHAN_ASR_MODEL="${STACKCHAN_ASR_MODEL:-glm-asr-2512}"
  export STACKCHAN_TTS_PROVIDER="${STACKCHAN_TTS_PROVIDER:-glm}"
  export STACKCHAN_TTS_MODEL="${STACKCHAN_TTS_MODEL:-glm-tts}"
  export STACKCHAN_TTS_VOICE="${STACKCHAN_TTS_VOICE:-tongtong}"
  export STACKCHAN_VISION_PROVIDER="${STACKCHAN_VISION_PROVIDER:-glm}"
  export STACKCHAN_VISION_MODEL="${STACKCHAN_VISION_MODEL:-glm-4.6v-flash}"

  if [[ "$STACKCHAN_ASR_PROVIDER" == "glm" ]]; then
    if [[ -z "${STACKCHAN_ASR_API_KEY:-$zhipu_key}" ]]; then
      echo "GLM ASR requires ZHIPU_API_KEY, GLM_API_KEY, or STACKCHAN_ASR_API_KEY." >&2
      return 1
    fi
    if [[ "${STACKCHAN_ASR_MODEL:-glm-asr-2512}" != glm-asr-* ]]; then
      echo "STACKCHAN_ASR_PROVIDER=glm requires a glm-asr-* model." >&2
      return 1
    fi
  fi
  if [[ "$STACKCHAN_TTS_PROVIDER" == "glm" ]]; then
    if [[ -z "${STACKCHAN_TTS_API_KEY:-$zhipu_key}" ]]; then
      echo "GLM TTS requires ZHIPU_API_KEY, GLM_API_KEY, or STACKCHAN_TTS_API_KEY." >&2
      return 1
    fi
    if [[ "${STACKCHAN_TTS_MODEL:-glm-tts}" != glm-tts* ]]; then
      echo "STACKCHAN_TTS_PROVIDER=glm requires a glm-tts model." >&2
      return 1
    fi
  fi
  if [[ "$STACKCHAN_VISION_PROVIDER" == "glm" ]]; then
    if [[ -z "${STACKCHAN_VISION_API_KEY:-$zhipu_key}" ]]; then
      echo "GLM Vision requires ZHIPU_API_KEY, GLM_API_KEY, or STACKCHAN_VISION_API_KEY." >&2
      return 1
    fi
    if [[ "${STACKCHAN_VISION_MODEL:-glm-4.6v-flash}" != glm-*v-* ]]; then
      echo "STACKCHAN_VISION_PROVIDER=glm requires a GLM vision model." >&2
      return 1
    fi
  fi

  if [[ -z "${STACKCHAN_ASR_COMMAND:-}" ]]; then
    case "${STACKCHAN_ASR_PROVIDER:-glm}" in
      glm) export STACKCHAN_ASR_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_asr_glm.py" ;;
      gemini) export STACKCHAN_ASR_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_asr_gemini.py" ;;
      openai) export STACKCHAN_ASR_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_asr_openai.py" ;;
    esac
    if [[ -z "${STACKCHAN_ASR_COMMAND:-}" && "${STACKCHAN_ENABLE_OPENAI_ASR:-0}" == "1" ]]; then
      export STACKCHAN_ASR_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_asr_openai.py"
    fi
  fi
  if [[ -z "${STACKCHAN_TTS_COMMAND:-}" ]]; then
    case "${STACKCHAN_TTS_PROVIDER:-glm}" in
      glm)
        export STACKCHAN_TTS_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_tts_glm.py"
        export STACKCHAN_TTS_STREAMING="${STACKCHAN_TTS_STREAMING:-1}"
        export STACKCHAN_TTS_COMMAND_STREAMING="${STACKCHAN_TTS_COMMAND_STREAMING:-1}"
        ;;
      gemini) export STACKCHAN_TTS_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_tts_gemini.py" ;;
      openai) export STACKCHAN_TTS_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_tts_openai.py" ;;
    esac
    if [[ -z "${STACKCHAN_TTS_COMMAND:-}" && "${STACKCHAN_ENABLE_OPENAI_TTS:-0}" == "1" ]]; then
      export STACKCHAN_TTS_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_tts_openai.py"
    fi
  fi
  if [[ -z "${STACKCHAN_VISION_COMMAND:-}" && "${STACKCHAN_VISION_PROVIDER:-glm}" == "glm" ]]; then
    export STACKCHAN_VISION_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_vision_glm.py"
  fi
}

configure_local_defaults() {
  local speech_token="${STACKCHAN_LOCAL_SPEECH_TOKEN:-local-no-secret}"
  export STACKCHAN_LOCAL_CHAT_BASE_URL="${STACKCHAN_LOCAL_CHAT_BASE_URL:-http://127.0.0.1:18080/v1}"
  export STACKCHAN_LOCAL_CHAT_API_KEY="${STACKCHAN_LOCAL_CHAT_API_KEY:-local-no-secret}"
  export STACKCHAN_LOCAL_CHAT_MODEL="${STACKCHAN_LOCAL_CHAT_MODEL:-Qwen3-4B}"
  export STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS="${STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS:-8192}"
  export STACKCHAN_LOCAL_CHAT_MAX_TOKENS="${STACKCHAN_LOCAL_CHAT_MAX_TOKENS:-256}"
  export STACKCHAN_LOCAL_MAX_MESSAGES="${STACKCHAN_LOCAL_MAX_MESSAGES:-8}"

  export STACKCHAN_LOCAL_ASR_BASE_URL="${STACKCHAN_LOCAL_ASR_BASE_URL:-http://127.0.0.1:18081/v1}"
  export STACKCHAN_LOCAL_ASR_API_KEY="${STACKCHAN_LOCAL_ASR_API_KEY:-$speech_token}"
  export STACKCHAN_LOCAL_ASR_MODEL="${STACKCHAN_LOCAL_ASR_MODEL:-SenseVoiceSmall}"
  export STACKCHAN_LOCAL_ASR_COMMAND="${STACKCHAN_LOCAL_ASR_COMMAND:-$VENV_PY $ROOT_DIR/scripts/stackchan_asr_openai.py}"

  export STACKCHAN_LOCAL_TTS_BASE_URL="${STACKCHAN_LOCAL_TTS_BASE_URL:-http://127.0.0.1:18081/v1}"
  export STACKCHAN_LOCAL_TTS_API_KEY="${STACKCHAN_LOCAL_TTS_API_KEY:-$speech_token}"
  export STACKCHAN_LOCAL_TTS_MODEL="${STACKCHAN_LOCAL_TTS_MODEL:-vits-melo-tts-zh_en}"
  export STACKCHAN_LOCAL_TTS_VOICE="${STACKCHAN_LOCAL_TTS_VOICE:-default}"
  export STACKCHAN_LOCAL_TTS_RESPONSE_FORMAT="${STACKCHAN_LOCAL_TTS_RESPONSE_FORMAT:-wav}"
  export STACKCHAN_LOCAL_TTS_COMMAND="${STACKCHAN_LOCAL_TTS_COMMAND:-$VENV_PY $ROOT_DIR/scripts/stackchan_tts_openai.py}"

  export STACKCHAN_LOCAL_VISION_BASE_URL="${STACKCHAN_LOCAL_VISION_BASE_URL:-http://127.0.0.1:18083/v1}"
  export STACKCHAN_LOCAL_VISION_API_KEY="${STACKCHAN_LOCAL_VISION_API_KEY:-local-no-secret}"
  export STACKCHAN_LOCAL_VISION_MODEL="${STACKCHAN_LOCAL_VISION_MODEL:-Qwen3-VL-4B-Instruct}"
  export STACKCHAN_LOCAL_VISION_COMMAND="${STACKCHAN_LOCAL_VISION_COMMAND:-$VENV_PY $ROOT_DIR/scripts/stackchan_vision_openai.py}"
  export STACKCHAN_LOCAL_VISION_ENABLED="${STACKCHAN_LOCAL_VISION_ENABLED:-0}"

  append_no_proxy_host "$STACKCHAN_LOCAL_CHAT_BASE_URL"
  append_no_proxy_host "$STACKCHAN_LOCAL_ASR_BASE_URL"
  append_no_proxy_host "$STACKCHAN_LOCAL_TTS_BASE_URL"
  append_no_proxy_host "$STACKCHAN_LOCAL_VISION_BASE_URL"
}

apply_local_mode() {
  CHAT_PROVIDER="local"
  export OPENAI_API_KEY="$STACKCHAN_LOCAL_CHAT_API_KEY"
  export OPENAI_BASE_URL="$STACKCHAN_LOCAL_CHAT_BASE_URL"
  export STACKCHAN_CHAT_MODEL="$STACKCHAN_LOCAL_CHAT_MODEL"
  export STACKCHAN_CHAT_THINKING=disabled
  export STACKCHAN_COMPACT_PROMPT="${STACKCHAN_LOCAL_COMPACT_PROMPT:-1}"
  export STACKCHAN_SAFE_NANOBOT_TOOLS="${STACKCHAN_LOCAL_SAFE_NANOBOT_TOOLS:-none}"

  export STACKCHAN_ASR_PROVIDER=local
  export STACKCHAN_ASR_BASE_URL="$STACKCHAN_LOCAL_ASR_BASE_URL"
  export STACKCHAN_ASR_API_KEY="$STACKCHAN_LOCAL_ASR_API_KEY"
  export STACKCHAN_ASR_MODEL="$STACKCHAN_LOCAL_ASR_MODEL"
  export STACKCHAN_ASR_COMMAND="$STACKCHAN_LOCAL_ASR_COMMAND"

  export STACKCHAN_TTS_PROVIDER=local
  export STACKCHAN_TTS_BASE_URL="$STACKCHAN_LOCAL_TTS_BASE_URL"
  export STACKCHAN_TTS_API_KEY="$STACKCHAN_LOCAL_TTS_API_KEY"
  export STACKCHAN_TTS_MODEL="$STACKCHAN_LOCAL_TTS_MODEL"
  export STACKCHAN_TTS_VOICE="$STACKCHAN_LOCAL_TTS_VOICE"
  export STACKCHAN_TTS_RESPONSE_FORMAT="$STACKCHAN_LOCAL_TTS_RESPONSE_FORMAT"
  export STACKCHAN_TTS_COMMAND="$STACKCHAN_LOCAL_TTS_COMMAND"
  export STACKCHAN_TTS_COMMAND_STREAMING="${STACKCHAN_LOCAL_TTS_COMMAND_STREAMING:-0}"

  if [[ "$STACKCHAN_LOCAL_VISION_ENABLED" == "1" ]]; then
    export STACKCHAN_VISION_PROVIDER=local
    export STACKCHAN_VISION_BASE_URL="$STACKCHAN_LOCAL_VISION_BASE_URL"
    export STACKCHAN_VISION_API_KEY="$STACKCHAN_LOCAL_VISION_API_KEY"
    export STACKCHAN_VISION_MODEL="$STACKCHAN_LOCAL_VISION_MODEL"
    export STACKCHAN_VISION_COMMAND="$STACKCHAN_LOCAL_VISION_COMMAND"
  else
    export STACKCHAN_VISION_PROVIDER=disabled
    export STACKCHAN_VISION_COMMAND=""
  fi
}

apply_hybrid_media() {
  export STACKCHAN_CLOUD_ASR_COMMAND="${STACKCHAN_ASR_COMMAND:-}"
  export STACKCHAN_CLOUD_TTS_COMMAND="${STACKCHAN_TTS_COMMAND:-}"
  export STACKCHAN_CLOUD_VISION_COMMAND="${STACKCHAN_VISION_COMMAND:-}"

  if [[ -n "$STACKCHAN_CLOUD_ASR_COMMAND" ]]; then
    export STACKCHAN_ASR_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_provider_fallback.py --kind asr"
  fi
  if [[ -n "$STACKCHAN_CLOUD_TTS_COMMAND" ]]; then
    export STACKCHAN_TTS_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_provider_fallback.py --kind tts"
    export STACKCHAN_TTS_COMMAND_STREAMING=1
  fi
  if [[ -n "$STACKCHAN_CLOUD_VISION_COMMAND" && "$STACKCHAN_LOCAL_VISION_ENABLED" == "1" ]]; then
    export STACKCHAN_VISION_COMMAND="$VENV_PY $ROOT_DIR/scripts/stackchan_provider_fallback.py --kind vision"
  fi
}

INFERENCE_MODE="${STACKCHAN_INFERENCE_MODE:-cloud}"
export STACKCHAN_INFERENCE_MODE="$INFERENCE_MODE"
case "$INFERENCE_MODE" in
  cloud)
    configure_cloud_chat
    configure_cloud_media
    ;;
  local)
    configure_local_defaults
    apply_local_mode
    ;;
  hybrid)
    configure_cloud_chat
    configure_cloud_media
    configure_local_defaults
    export STACKCHAN_COMPACT_PROMPT="${STACKCHAN_HYBRID_COMPACT_PROMPT:-1}"
    apply_hybrid_media
    ;;
  *)
    echo "STACKCHAN_INFERENCE_MODE must be cloud, local, or hybrid; got: $INFERENCE_MODE" >&2
    return 1
    ;;
esac

if [[ "$INFERENCE_MODE" != "cloud" && "${STACKCHAN_SESSION_MODE_SUFFIX:-1}" == "1" ]]; then
  SESSION_NAMESPACE_BASE="${STACKCHAN_SESSION_NAMESPACE:-stackchan}"
  if [[ "$SESSION_NAMESPACE_BASE" != *":$INFERENCE_MODE" ]]; then
    export STACKCHAN_SESSION_NAMESPACE="$SESSION_NAMESPACE_BASE:$INFERENCE_MODE"
  fi
  SESSION_SCHEMA="${STACKCHAN_SESSION_SCHEMA_VERSION:-ux2}"
  if [[ "$STACKCHAN_SESSION_NAMESPACE" != *":$SESSION_SCHEMA" ]]; then
    export STACKCHAN_SESSION_NAMESPACE="$STACKCHAN_SESSION_NAMESPACE:$SESSION_SCHEMA"
  fi
fi

case "$INFERENCE_MODE" in
  cloud)
    export NANOBOT_OPENAI_COMPAT_TIMEOUT_S="${STACKCHAN_CHAT_TIMEOUT:-20}"
    ;;
  local)
    export NANOBOT_OPENAI_COMPAT_TIMEOUT_S="${STACKCHAN_LOCAL_CHAT_TIMEOUT_S:-60}"
    ;;
  hybrid)
    export NANOBOT_OPENAI_COMPAT_TIMEOUT_S="${STACKCHAN_HYBRID_CHAT_TIMEOUT_S:-15}"
    ;;
esac

if [[ "${STACKCHAN_BYPASS_PROVIDER_PROXY:-0}" == "1" ]]; then
  append_no_proxy_host "$OPENAI_BASE_URL"
  append_no_proxy_host "${STACKCHAN_GLM_BASE_URL:-https://open.bigmodel.cn/api/paas/v4}"
fi
