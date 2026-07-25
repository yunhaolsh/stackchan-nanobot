# StackChan Nanobot

This repository contains the host-side StackChan Bridge, Nanobot integration,
cloud model adapters, tests, and deployment documentation. Device firmware is
maintained in the `StackChan` submodule so its ESP-IDF history and upstream
relationship remain intact.

## Repository Layout

- `nanobot_bridge/`: Xiaozhi WebSocket, MCP gateway, sessions, and Nanobot runtime.
- `scripts/`: startup, provider adapters, flashing, and diagnostic commands.
- `nanobot_config/`: checked-in configuration templates; runtime state is ignored.
- `StackChan/`: firmware fork pinned to the `stackchan-nanobot` branch.
- `StackChan-BSP/`: upstream board support package submodule.
- `docs/`: architecture, implementation history, and verification records.

## Setup

Clone with submodules and create the host environment:

```bash
git clone --recurse-submodules git@github.com:yunhaolsh/stackchan-nanobot.git
cd stackchan-nanobot
python3 -m venv .venv-nanobot
.venv-nanobot/bin/pip install -r requirements-dev.txt
cp nanobot_config/stackchan-nanobot.env.example .run/stackchan-nanobot.env
```

Keep API keys only in the ignored `.run/stackchan-nanobot.env` file. Start and
stop the service with:

```bash
STACKCHAN_ENV_FILE="$PWD/.run/stackchan-nanobot.env" ./scripts/start_stackchan_nanobot_hotspot.sh
./scripts/stop_stackchan_nanobot.sh
```

Run host tests with `.venv-nanobot/bin/pytest -q`. Firmware build and flashing
instructions are maintained in `environment_setup.md` and `docs/`.

Enable the repository secret guard after cloning:

```bash
git config core.hooksPath .githooks
```
