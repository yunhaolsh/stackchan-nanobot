#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${STACKCHAN_PORT:-/dev/ttyACM0}"
SECONDS="${STACKCHAN_CAPTURE_SECONDS:-60}"
PYTHON="$ROOT_DIR/.venv-nanobot/bin/python"
DIAGNOSE="$ROOT_DIR/scripts/diagnose_stackchan_serial.py"
OUTPUT="$ROOT_DIR/.run/latest-agent-boot.log"

if [[ ! -x "$PYTHON" ]]; then
    printf 'Python environment not found: %s\n' "$PYTHON" >&2
    exit 1
fi

if [[ ! -e "$PORT" ]]; then
    printf 'Serial port not found: %s\n' "$PORT" >&2
    printf 'Connect USB. If the port only appears in Download mode, enter Download mode and rerun this command.\n' >&2
    exit 1
fi

mkdir -p "$ROOT_DIR/.run"
printf 'Capturing %s for %s seconds. Release BOOT and press Reset once to boot normally.\n' "$PORT" "$SECONDS"
sudo "$PYTHON" "$DIAGNOSE" --port "$PORT" --seconds "$SECONDS" | tee "$OUTPUT"
printf 'Saved: %s\n' "$OUTPUT"
