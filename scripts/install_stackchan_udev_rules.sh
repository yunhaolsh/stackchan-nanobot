#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_RULE="$ROOT_DIR/config/udev/77-stackchan-usb.rules"
TARGET_RULE="/etc/udev/rules.d/77-stackchan-usb.rules"

if [[ ! -f "$SOURCE_RULE" ]]; then
  echo "Missing udev rule: $SOURCE_RULE" >&2
  exit 1
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

install -m 0644 "$SOURCE_RULE" "$TARGET_RULE"
udevadm control --reload-rules
udevadm trigger --action=change --subsystem-match=tty || true
udevadm trigger --action=change --subsystem-match=usb \
  --attr-match=idVendor=303a --attr-match=idProduct=1001 || true

echo "Installed $TARGET_RULE"
echo "Unplug and reconnect StackChan USB once before the next serial diagnosis."
