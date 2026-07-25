#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for name in stackchan-local-llm stackchan-local-speech; do
  pid_file="$ROOT_DIR/.run/$name.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "$name not tracked"
    continue
  fi
  pid="$(cat "$pid_file")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Stopping $name pid=$pid"
    kill "$pid"
    for _ in $(seq 1 20); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "$name did not stop after 2s" >&2
      exit 1
    fi
  fi
  rm -f "$pid_file"
done
