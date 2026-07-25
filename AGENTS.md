# Repository Guidelines

## Project Structure & Module Organization

The host and firmware are deliberately separate. `nanobot_bridge/` owns the
Xiaozhi WebSocket transport, per-device sessions, MCP request correlation,
capability policy, and the embedded Nanobot runtime. Provider-specific audio and
vision adapters live under `scripts/`; they communicate with the Bridge through
environment variables and length-prefixed Opus streams. `StackChan/` is a
submodule containing the ESP-IDF firmware fork, while `StackChan-BSP/` tracks the
upstream board support package. Treat both as independent repositories and do
not vendor ESP-IDF or model weights into this repository.

## Build, Test, and Development Commands

Install host dependencies with `.venv-nanobot/bin/pip install -r
requirements-dev.txt`. Run the complete host suite using
`.venv-nanobot/bin/pytest -q`, or one module with `.venv-nanobot/bin/pytest -q
tests/test_bridge_sessions.py`. Start the LAN service through
`STACKCHAN_ENV_FILE="$PWD/.run/stackchan-nanobot.env"
./scripts/start_stackchan_nanobot_hotspot.sh`; stop it with
`./scripts/stop_stackchan_nanobot.sh`. For firmware, source `esp-idf/export.sh`,
change to `StackChan/firmware`, and run `idf.py build`.

## Coding Style & Naming Conventions

Python uses four-space indentation, type hints for shared interfaces, and
`snake_case` names. Keep provider adapters small and preserve their existing
stdin/file contracts. Shell scripts use Bash strict mode (`set -euo pipefail`)
and resolve paths relative to the repository root. C++ changes belong inside
the firmware submodule and should follow its existing style.

## Testing Guidelines

Pytest tests live in `tests/` and use `test_*.py` naming. Extend protocol tests
when changing WebSocket, MCP, audio framing, provider fallback, or permission
behavior. A firmware change is not complete until `idf.py build` succeeds.

## Security and Commits

Never commit API keys, private keys, `.run/`, Nanobot sessions, model weights,
or generated firmware builds. Enable `.githooks` and run
`python3 scripts/check_no_secrets.py --staged` before committing. Use concise
imperative commit subjects such as `feat: add local inference mode` and commit
firmware changes in `StackChan` before updating the parent submodule pointer.
