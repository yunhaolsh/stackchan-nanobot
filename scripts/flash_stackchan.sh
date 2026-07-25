#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${STACKCHAN_PORT:-/dev/ttyACM0}"
ESPTOOL_PY="${ESPTOOL_PY:-/home/yunhao/.espressif/python_env/idf5.5_py3.13_env/bin/python}"
BUILD_DIR="$ROOT_DIR/StackChan/firmware/build"

if [[ ! -e "$PORT" ]]; then
  echo "Serial port not found: $PORT" >&2
  echo "Set STACKCHAN_PORT=/dev/ttyACM1 if the device enumerated on another port." >&2
  exit 1
fi

if [[ ! -f "$BUILD_DIR/flash_args" || ! -f "$BUILD_DIR/stack-chan.bin" ]]; then
  echo "Missing firmware build output under $BUILD_DIR" >&2
  echo "Run: . $ROOT_DIR/esp-idf/export.sh && cd $ROOT_DIR/StackChan/firmware && idf.py build" >&2
  exit 1
fi

cd "$BUILD_DIR"

if [[ -r "$PORT" && -w "$PORT" ]]; then
  exec "$ESPTOOL_PY" -m esptool \
    --chip esp32s3 -p "$PORT" -b 460800 \
    --before default_reset --after hard_reset \
    write_flash @flash_args
fi

echo "Serial port $PORT requires elevated permissions; invoking sudo."
exec sudo "$ESPTOOL_PY" -m esptool \
  --chip esp32s3 -p "$PORT" -b 460800 \
  --before default_reset --after hard_reset \
  write_flash @flash_args
