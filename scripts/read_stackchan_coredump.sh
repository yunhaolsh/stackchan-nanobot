#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${STACKCHAN_PORT:-/dev/ttyACM0}"
IDF_PATH="${STACKCHAN_IDF_PATH:-$ROOT_DIR/esp-idf}"
IDF_TOOLS_PATH="${IDF_TOOLS_PATH:-$HOME/.espressif}"
IDF_PYTHON="$IDF_TOOLS_PATH/python_env/idf5.5_py3.13_env/bin/python"
FIRMWARE_DIR="$ROOT_DIR/StackChan/firmware"
RAW_CORE_FILE="$ROOT_DIR/.run/timer-crash-coredump.bin"
LOG_FILE="$ROOT_DIR/.run/timer-crash-coredump.log"

if [[ ! -c "$PORT" ]]; then
    printf 'Serial port not found: %s\n' "$PORT" >&2
    exit 1
fi

if [[ ! -x "$IDF_PYTHON" ]]; then
    printf 'ESP-IDF Python not found: %s\n' "$IDF_PYTHON" >&2
    exit 1
fi

mkdir -p "$ROOT_DIR/.run"

printf 'Saving raw StackChan coredump from %s...\n' "$PORT"
sudo env \
    HOME="$HOME" \
    IDF_PATH="$IDF_PATH" \
    IDF_TOOLS_PATH="$IDF_TOOLS_PATH" \
    IDF_PYTHON_ENV_PATH="$IDF_TOOLS_PATH/python_env/idf5.5_py3.13_env" \
    PATH="$IDF_TOOLS_PATH/tools:$PATH" \
    "$IDF_PYTHON" -m esptool \
    --chip esp32s3 \
    --port "$PORT" \
    --before no_reset \
    --after no_reset \
    read_flash 0xf00000 0x10000 "$RAW_CORE_FILE"
sudo chown "$(id -u):$(id -g)" "$RAW_CORE_FILE"

printf 'Raw coredump saved: %s\n' "$RAW_CORE_FILE"
printf 'Attempting decode with current firmware symbols...\n'
set +e
sudo env \
    HOME="$HOME" \
    IDF_PATH="$IDF_PATH" \
    IDF_TOOLS_PATH="$IDF_TOOLS_PATH" \
    IDF_PYTHON_ENV_PATH="$IDF_TOOLS_PATH/python_env/idf5.5_py3.13_env" \
    PATH="$IDF_TOOLS_PATH/tools:$PATH" \
    "$IDF_PYTHON" "$IDF_PATH/tools/idf.py" \
    -C "$FIRMWARE_DIR" \
    coredump-info \
    --core "$RAW_CORE_FILE" \
    2>&1 | tee "$LOG_FILE"
decode_status=${PIPESTATUS[0]}
set -e

printf 'Saved report: %s\n' "$LOG_FILE"
exit "$decode_status"
