#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv-nanobot/bin/python"
ENV_FILE="${STACKCHAN_ENV_FILE:-$ROOT_DIR/.run/stackchan-nanobot.env}"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Missing Nanobot virtualenv Python: $VENV_PY" >&2
  exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

echo "== shell syntax =="
bash -n \
  scripts/start_stackchan_nanobot.sh \
  scripts/start_stackchan_nanobot_hotspot.sh \
  scripts/stop_stackchan_nanobot.sh \
  scripts/flash_stackchan.sh \
  scripts/verify_stackchan_nanobot_local.sh

echo
echo "== python syntax =="
"$VENV_PY" -m py_compile \
  nanobot_bridge/audio_endpoint.py \
  nanobot_bridge/server.py \
  nanobot_bridge/capabilities.py \
  nanobot_bridge/mcp_http.py \
  nanobot_bridge/nanobot_runtime.py \
  scripts/check_stackchan_nanobot_runtime.py \
  scripts/diagnose_stackchan_nanobot_live.py \
  scripts/stackchan_asr_openai.py \
  scripts/stackchan_asr_gemini.py \
  scripts/stackchan_tts_openai.py \
  scripts/stackchan_tts_gemini.py \
  scripts/stackchan_audio.py \
  scripts/stackchan_asr_glm.py \
  scripts/stackchan_tts_glm.py \
  scripts/stackchan_vision_glm.py \
  scripts/test_stackchan_bridge_protocol.py \
  scripts/test_stackchan_mcp_protocol.py \
  scripts/test_stackchan_nanobot_tool_loop.py \
  tests/test_bridge_capabilities.py

echo
echo "== runtime prerequisites, local only =="
"$VENV_PY" scripts/check_stackchan_nanobot_runtime.py

echo
echo "== bridge protocol, offline fake ASR/TTS =="
"$VENV_PY" scripts/test_stackchan_bridge_protocol.py

echo
echo "== tool router and permission policy =="
"$VENV_PY" -m pytest -q \
  tests/test_audio_endpoint.py \
  tests/test_tts_streaming.py \
  tests/test_mdns_alias.py \
  tests/test_glm_http.py \
  tests/test_bridge_capabilities.py \
  tests/test_timer_persistence.py

echo
echo "== MCP discovery, pagination, proxy, and permissions =="
"$VENV_PY" scripts/test_stackchan_mcp_protocol.py

echo
echo "== Nanobot-owned model and tool-call loop =="
"$VENV_PY" scripts/test_stackchan_nanobot_tool_loop.py

echo
echo "== secret scan =="
if rg -n "sk-[A-Za-z0-9_-]{20,}|OPENAI_API_KEY=sk-[A-Za-z0-9_-]{20,}|AZURE_OPENAI_API_KEY=[A-Za-z0-9_-]{20,}|CODEX_API_KEY=sk-[A-Za-z0-9_-]{20,}" \
  nanobot_bridge scripts nanobot_config environment_setup.md -S; then
  echo "Potential secret-like value found; review output above." >&2
  exit 1
fi
echo "No secret-like values found."
if [[ -f .run/stackchan-nanobot.env ]]; then
  echo "Local env file present: .run/stackchan-nanobot.env"
  echo "It was loaded for this verification and is intentionally not scanned or printed."
fi

echo
echo "Local StackChan Nanobot verification passed."
