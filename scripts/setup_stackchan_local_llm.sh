#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${STACKCHAN_LOCAL_RUNTIME_DIR:-$ROOT_DIR/.local-runtime}"
SOURCE_DIR="$RUNTIME_DIR/llama.cpp"
BUILD_DIR="${STACKCHAN_LLAMA_BUILD_DIR:-$SOURCE_DIR/build-stackchan}"
GPU_BACKEND="${STACKCHAN_LLAMA_GPU_BACKEND:-cpu}"
LLAMA_CPP_REF="${STACKCHAN_LLAMA_CPP_REF:-b9631}"
LLAMA_CPP_COMMIT="${STACKCHAN_LLAMA_CPP_COMMIT:-6e14286edaa60a223292c8a996506905b2f66f66}"
MODEL_DIR="${STACKCHAN_LOCAL_LLM_MODEL_DIR:-$ROOT_DIR/models/llm}"
MODEL_NAME="Qwen3-4B-Q4_K_M.gguf"
MODEL_PATH="${STACKCHAN_LLAMA_MODEL_PATH:-$MODEL_DIR/$MODEL_NAME}"
MODEL_REVISION="${STACKCHAN_LLAMA_MODEL_REVISION:-bc640142c66e1fdd12af0bd68f40445458f3869b}"
MODEL_URL="${STACKCHAN_LLAMA_MODEL_URL:-https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/$MODEL_REVISION/$MODEL_NAME?download=true}"
MODEL_SHA256="${STACKCHAN_LLAMA_MODEL_SHA256:-7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5}"

mkdir -p "$RUNTIME_DIR"
if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  git clone --depth 1 --branch "$LLAMA_CPP_REF" \
    https://github.com/ggml-org/llama.cpp.git "$SOURCE_DIR"
else
  git -C "$SOURCE_DIR" fetch --depth 1 origin "$LLAMA_CPP_REF"
  git -C "$SOURCE_DIR" checkout --detach FETCH_HEAD
fi
if [[ -n "$LLAMA_CPP_COMMIT" ]]; then
  test "$(git -C "$SOURCE_DIR" rev-parse HEAD)" = "$LLAMA_CPP_COMMIT"
fi

cmake_args=(
  -DGGML_NATIVE=ON
  -DLLAMA_BUILD_TESTS=OFF
  -DLLAMA_BUILD_EXAMPLES=OFF
  -DLLAMA_BUILD_APP=OFF
  -DLLAMA_BUILD_TOOLS=ON
  -DLLAMA_BUILD_SERVER=ON
  -DLLAMA_BUILD_UI=OFF
  -DLLAMA_USE_PREBUILT_UI=OFF
)
case "$GPU_BACKEND" in
  cpu) cmake_args+=(-DGGML_CUDA=OFF) ;;
  cuda) cmake_args+=(-DGGML_CUDA=ON) ;;
  *) echo "STACKCHAN_LLAMA_GPU_BACKEND must be cpu or cuda" >&2; exit 1 ;;
esac

cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" "${cmake_args[@]}"
cmake --build "$BUILD_DIR" --config Release \
  --target llama-server \
  -j "${STACKCHAN_BUILD_JOBS:-4}"
test -x "$BUILD_DIR/bin/llama-server"
echo "llama-server is ready: $BUILD_DIR/bin/llama-server"

mkdir -p "$MODEL_DIR" "$(dirname "$MODEL_PATH")"
if [[ ! -f "$MODEL_PATH" ]]; then
  echo "Downloading $MODEL_NAME with resume support..."
  curl -fL --progress-bar --retry 3 --retry-delay 2 -C - \
    "$MODEL_URL" \
    -o "$MODEL_PATH.part"
  printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PATH.part" | sha256sum --check -
  mv "$MODEL_PATH.part" "$MODEL_PATH"
fi
printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PATH" | sha256sum --check -
echo "Local LLM model is ready: $MODEL_PATH"
