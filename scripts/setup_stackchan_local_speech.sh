#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${STACKCHAN_LOCAL_SPEECH_VENV:-$ROOT_DIR/.venv-local-speech}"
MODEL_ROOT="${STACKCHAN_LOCAL_SPEECH_MODEL_ROOT:-$ROOT_DIR/models/speech}"

resolve_python() {
  if [[ -n "${STACKCHAN_LOCAL_PYTHON:-}" ]]; then
    printf '%s\n' "$STACKCHAN_LOCAL_PYTHON"
    return
  fi

  local candidate
  for candidate in python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 12)))' \
        >/dev/null 2>&1; then
      command -v "$candidate"
      return
    fi
  done

  if command -v pyenv >/dev/null 2>&1; then
    local pyenv_version
    pyenv_version="$(pyenv versions --bare 2>/dev/null | awk '/^3\.12\.[0-9]+$/ { print }' | sort -V | tail -1)"
    if [[ -n "$pyenv_version" ]]; then
      PYENV_VERSION="$pyenv_version" pyenv which python
      return
    fi
  fi
  return 1
}

if ! PYTHON="$(resolve_python)"; then
  echo "Python 3.10-3.12 is required for the sherpa-onnx environment." >&2
  echo "Set STACKCHAN_LOCAL_PYTHON to a supported Python executable." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 12)))'; then
  echo "Unsupported local speech Python: $PYTHON" >&2
  exit 1
fi
echo "Using local speech Python: $PYTHON"

"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements-local-speech.txt"

mkdir -p "$MODEL_ROOT"

download_model() {
  local url="$1"
  local directory="$2"
  local required_file="$3"
  local required_sha256="$4"
  if [[ -f "$MODEL_ROOT/$directory/$required_file" ]]; then
    printf '%s  %s\n' "$required_sha256" "$MODEL_ROOT/$directory/$required_file" \
      | sha256sum --check -
    echo "Model already present: $directory"
    return
  fi
  local archive
  archive="$(mktemp "$MODEL_ROOT/.download-XXXXXX.tar.bz2")"
  curl -fL --retry 3 --retry-delay 2 "$url" -o "$archive"
  tar -xjf "$archive" -C "$MODEL_ROOT"
  rm -f "$archive"
  if [[ ! -f "$MODEL_ROOT/$directory/$required_file" ]]; then
    echo "Model archive did not produce $directory/$required_file" >&2
    exit 1
  fi
  printf '%s  %s\n' "$required_sha256" "$MODEL_ROOT/$directory/$required_file" \
    | sha256sum --check -
}

download_model \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2" \
  "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17" \
  "model.int8.onnx" \
  "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"

download_model \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2" \
  "vits-melo-tts-zh_en" \
  "model.onnx" \
  "bf30582eb1b012250a35b1a4a80e7dfbcf8485e7bb9de0d95efbbeef0e4ad86d"

"$VENV_DIR/bin/python" "$ROOT_DIR/local_inference/speech_service.py" --check
echo "Local speech runtime is ready."
