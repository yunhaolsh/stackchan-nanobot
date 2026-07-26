# StackChan firmware setup log

> 2026-07-24 update: the current GLM/Nanobot/MCP implementation and portable
> runbook are documented in `nanobot_bridge/README.md` and
> `docs/stackchan-nanobot-glm-implementation.md`. Sections below preserve the
> original bring-up history and may describe older Gemini or direct-IP tests.

Date: 2026-07-17

## Goal

Flash official StackChan firmware to the connected M5Stack StackChan device, then use it as the device-side client for a Nanobot/OpenClaw style server-side brain later.

Important conclusion: Nanobot is not flashed directly into the ESP32-S3 firmware. The device runs StackChan firmware. Nanobot/OpenClaw/Agora style logic should run on a server or PC and communicate with the device through the firmware-supported network/AI-agent path.

## Local paths

- Workspace: `/home/yunhao/github/stackchan`
- ESP-IDF: `/home/yunhao/github/stackchan/esp-idf`
- StackChan firmware: `/home/yunhao/github/stackchan/StackChan/firmware`

## ESP-IDF setup

The ESP-IDF git clone failed because GitHub disconnected during clone. The workaround was downloading the v5.5.4 tarball from:

```bash
https://github.com/espressif/esp-idf/archive/refs/tags/v5.5.4.tar.gz
```

Because GitHub source tarballs do not contain git submodules, `git submodule update` fails with:

```text
fatal: 不是 Git 仓库（或者任何父目录）：.git
```

This is expected for a downloaded tarball. The missing ESP-IDF submodule directories were filled by downloading each submodule's exact v5.5.4 commit tarball using the GitHub API.

Then ESP-IDF tools were installed with:

```bash
cd /home/yunhao/github/stackchan/esp-idf
./install.sh esp32s3
```

## Firmware dependency setup

StackChan firmware dependencies were fetched from:

```bash
cd /home/yunhao/github/stackchan/StackChan/firmware
python3 ./fetch_repos.py
```

The script successfully cloned the firmware components and applied the `xiaozhi-esp32` patch. Re-running it may report that the patch cannot be applied cleanly; that is expected if the patch was already applied.

## Build

Build command:

```bash
. /home/yunhao/github/stackchan/esp-idf/export.sh
cd /home/yunhao/github/stackchan/StackChan/firmware
idf.py build
```

Build result:

```text
Generated /home/yunhao/github/stackchan/StackChan/firmware/build/stack-chan.bin
stack-chan.bin binary size 0x39c4e0 bytes.
Smallest app partition is 0x4f0000 bytes.
0x153b20 bytes (27%) free.
Project build complete.
```

## Device and flashing

Detected USB device:

```text
303a:1001 Espressif USB JTAG/serial debug unit
```

Detected serial port:

```text
/dev/ttyACM0
```

On Linux, install the repository udev rule once before long-running serial
diagnostics. It prevents ModemManager from probing the ESP32-S3 CDC endpoint
and disables autosuspend for this VID/PID only:

```bash
cd /home/yunhao/github/stackchan
./scripts/install_stackchan_udev_rules.sh
```

Unplug and reconnect the StackChan USB cable once after installation. Without
this rule, `udevadm info` may report `ID_MM_CANDIDATE=1`; ModemManager can then
toggle USB modem-control lines and cause `USB_UART_CHIP_RESET` while the
firmware is otherwise running normally.

Current user is not in the `dialout` group, so flashing required `sudo`.

Flash command used:

```bash
cd /home/yunhao/github/stackchan/StackChan/firmware/build
sudo /home/yunhao/.espressif/python_env/idf5.5_py3.13_env/bin/python -m esptool \
  --chip esp32s3 -p /dev/ttyACM0 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash @flash_args
```

Flash result:

```text
Chip is ESP32-S3 (QFN56) (revision v0.2)
MAC: 44:1b:f6:e5:62:28
Hash of data verified.
Leaving...
Hard resetting via RTS pin...
```

The firmware was written and verified successfully.

## Current state

After the first flash, serial monitor showed:

```text
boot:0x23 (DOWNLOAD(USB/UART0))
waiting for download
```

This means the device is still booting in download mode. The firmware is flashed, but the device must be restarted normally:

1. Release the BOOT/download button.
2. Press Reset, or unplug and replug USB.
3. Do not hold BOOT/download while powering on.

After a normal reboot, the firmware should start and show StackChan setup/UI behavior.

## Local Nanobot bridge

A minimal local bridge was added under:

```text
/home/yunhao/github/stackchan/nanobot_bridge
```

The correct Nanobot package is `nanobot-ai`. The unrelated PyPI package named `nanobot` was removed from the local virtual environment.

Installed local Nanobot environment:

```bash
cd /home/yunhao/github/stackchan
python3 -m venv .venv-nanobot
.venv-nanobot/bin/python -m pip install nanobot-ai
```

Verified:

```text
nanobot v0.2.2
```

Local Nanobot config:

```text
/home/yunhao/github/stackchan/nanobot_config/config.json
```

The config uses environment variable references:

```text
${OPENAI_API_KEY}
${OPENAI_BASE_URL}
```

so API secrets are not written into the repo.

Run command:

```bash
cd /home/yunhao/github/stackchan
.venv-nanobot/bin/python nanobot_bridge/server.py \
  --host 0.0.0.0 \
  --port 12800 \
  --public-host stackchan-nanobot.local \
  --nanobot-config /home/yunhao/github/stackchan/nanobot_config/config.json
```

Self-test endpoints:

```bash
curl http://127.0.0.1:12800/health
curl -X POST http://127.0.0.1:12800/xiaozhi/ota/ -H 'Content-Type: application/json' -d '{}'
```

The bridge returns Xiaozhi-compatible OTA protocol config:

```json
{
  "firmware": {},
  "websocket": {
    "url": "ws://stackchan-nanobot.local:12800/ws",
    "token": "hi-stack-chan",
    "version": 3
  },
  "server_time": {
    "timestamp": 0,
    "timezone_offset": 480
  }
}
```

The bridge WebSocket hello path was tested locally with a WebSocket client. It returned:

```text
{"type":"hello","transport":"websocket",...}
{"type":"llm","emotion":"happy"}
{"type":"tts","state":"sentence_start","text":"本地 Nanobot bridge 已连接"}
```

Nanobot OpenAI-compatible API server was also started:

```bash
.venv-nanobot/bin/nanobot serve \
  --config /home/yunhao/github/stackchan/nanobot_config/config.json \
  --host 127.0.0.1 \
  --port 8900
```

Endpoint:

```text
http://127.0.0.1:8900/v1/chat/completions
```

No real model request was executed during this verification, because doing so sends prompt/workspace context to the configured external OpenAI-compatible endpoint and requires explicit approval.

Helper scripts:

```bash
# Start or reuse both local services
/home/yunhao/github/stackchan/scripts/start_stackchan_nanobot.sh

# Stop services started by the helper
/home/yunhao/github/stackchan/scripts/stop_stackchan_nanobot.sh

# Read serial without forcing a reset and identify download mode
sudo /home/yunhao/github/stackchan/.venv-nanobot/bin/python \
  /home/yunhao/github/stackchan/scripts/diagnose_stackchan_serial.py \
  --port /dev/ttyACM0 --seconds 8
```

The diagnostic reader uses a read-only Linux file descriptor and never issues
DTR/RTS ioctls. `idf.py monitor` and general-purpose serial terminals may reset
the ESP32-S3 unless their no-reset/passive mode is explicitly enabled.

Current service state verified by the helper:

```text
nanobot-api already responding: http://127.0.0.1:8900/v1/chat/completions (http 405)
stackchan-bridge already responding: http://127.0.0.1:12800/health (http 200)
```

Latest serial diagnostic:

```text
Device is still in ESP32-S3 download mode.
Release BOOT/download, then press Reset. If it persists, GPIO0/BOOT is being held low.
```

The firmware was reconfigured to point at this bridge:

```text
CONFIG_STACKCHAN_SERVER_URL="http://stackchan-nanobot.local:12800"
CONFIG_OTA_URL="http://stackchan-nanobot.local:12800/xiaozhi/ota/"
CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES=y
```

## Current Nanobot runtime plan

The working network path uses the Windows hotspot / reachable PC IP directly,
not mDNS:

```text
PC / bridge IP: 192.168.137.247
StackChan IP: DHCP address on 192.168.137.0/24
OTA URL: http://192.168.137.247:12800/xiaozhi/ota/
WS URL: ws://192.168.137.247:12800/ws
```

The firmware was patched so the OTA URL comes from `CONFIG_OTA_URL` instead of
stale NVS. The direct-IP build was verified by the bridge log receiving a
StackChan OTA request from the device.

Bridge capabilities now implemented:

- Xiaozhi OTA response with WebSocket v3 config.
- WebSocket hello handshake.
- Xiaozhi v3 binary audio parsing; the bridge strips the 4-byte v3 header and
  buffers real Opus packets after `listen start`.
- Server-side WebRTC VAD closes `auto` listening turns because this firmware
  does not send `listen.stop` in auto mode.
- Nanobot text call through `nanobot-ai`.
- ASR hook via `STACKCHAN_ASR_COMMAND`.
- OpenAI-compatible ASR helper: `scripts/stackchan_asr_openai.py`.
- TTS hook via `STACKCHAN_TTS_COMMAND`.
- OpenAI-compatible TTS helper: `scripts/stackchan_tts_openai.py`.
- TTS reply packets are wrapped as Xiaozhi WebSocket v3 binary frames and paced
  at 60 ms per packet so the ESP32's 2400 ms decode queue cannot overflow.
- Runtime checks: `scripts/check_stackchan_nanobot_runtime.py`.
- Live device diagnosis: `scripts/diagnose_stackchan_nanobot_live.py`.

Recommended local prerequisite check:

```bash
cd /home/yunhao/github/stackchan
./scripts/verify_stackchan_nanobot_local.sh
```

The local verifier runs shell syntax checks, Python syntax checks, local runtime
prerequisites, the offline bridge protocol regression test, and a secret scan.

Individual local checks:

```bash
.venv-nanobot/bin/python scripts/check_stackchan_nanobot_runtime.py
.venv-nanobot/bin/python scripts/test_stackchan_bridge_protocol.py
```

Optional external model endpoint check. This calls the configured chat, ASR,
and TTS endpoints:

```bash
STACKCHAN_CHAT_MODEL=gemini-2.5-flash \
STACKCHAN_ASR_PROVIDER=gemini \
STACKCHAN_ASR_MODEL=gemini-2.5-flash \
STACKCHAN_TTS_PROVIDER=gemini \
STACKCHAN_TTS_MODEL=gemini-2.5-flash-preview-tts \
.venv-nanobot/bin/python scripts/check_stackchan_nanobot_runtime.py --external
```

To probe multiple chat model names and print the first usable runtime value:

```bash
.venv-nanobot/bin/python scripts/check_stackchan_nanobot_runtime.py --external
```

Recommended runtime command:

```bash
cd /home/yunhao/github/stackchan
cp nanobot_config/stackchan-nanobot.env.example .run/stackchan-nanobot.env
# edit .run/stackchan-nanobot.env if you need different keys/models/endpoints
./scripts/start_stackchan_nanobot_hotspot.sh
```

Equivalent explicit command:

```bash
cd /home/yunhao/github/stackchan

STACKCHAN_PUBLIC_HOST=stackchan-nanobot.local \
STACKCHAN_RESTART_BRIDGE=1 \
STACKCHAN_RESTART_NANOBOT=1 \
STACKCHAN_CHAT_MODEL=gemini-2.5-flash \
STACKCHAN_ASR_PROVIDER=gemini \
STACKCHAN_ASR_MODEL=gemini-2.5-flash \
STACKCHAN_TTS_PROVIDER=gemini \
STACKCHAN_TTS_MODEL=gemini-2.5-flash-preview-tts \
STACKCHAN_TTS_VOICE=Kore \
./scripts/start_stackchan_nanobot.sh
```

Then enter the Agent app on StackChan and run:

```bash
.venv-nanobot/bin/python scripts/diagnose_stackchan_nanobot_live.py --watch-seconds 120
```

To push a display-only test message to the device:

```bash
.venv-nanobot/bin/python scripts/diagnose_stackchan_nanobot_live.py \
  --say "StackChan Nanobot live check"
```

Success evidence in `.run/stackchan-bridge.log`:

```text
[ota] ...
[ws] connected from 192.168.137.x
[listen] start ...
[ws] audio frame bytes=... listening=True buffered=...
[vad] endpoint reason=end_silence frames=... duration_ms=...
[asr] transcript='...'
[tts] generated opus packets=...
```

Known runtime risks:

- `GEMINI_API_KEY` is local-only. Keep it in `.run/stackchan-nanobot.env`.
- `STACKCHAN_PUBLIC_HOST` defaults to `stackchan-nanobot.local`; the start
  script publishes that mDNS name to the bridge machine's current IP.
- If a router or hotspot blocks mDNS, set `STACKCHAN_PUBLIC_HOST` to a direct
  IP and rebuild/reflash firmware for that IP.
- If ASR or TTS fails, run `check_stackchan_nanobot_runtime.py --external`
  before debugging the device path.
- On a new machine, copy the env example, fill `GEMINI_API_KEY`, set the new
  `STACKCHAN_PUBLIC_HOST`, then restart the bridge. Reflash only if the
  firmware points to an old host/IP.

The active compiled config was verified in:

```text
StackChan/firmware/build/config/sdkconfig.h
```

The hostname-based firmware was built successfully. Flash it with the helper:

```bash
cd /home/yunhao/github/stackchan
./scripts/flash_stackchan.sh
```

The helper uses `/dev/ttyACM0` by default and invokes `sudo` only if the serial port is not readable/writable by the current user. After flashing and resetting the device, it should request:

```text
http://stackchan-nanobot.local:12800/xiaozhi/ota/
```

and then connect to:

```text
ws://stackchan-nanobot.local:12800/ws
```

## Useful follow-up commands

Open monitor:

```bash
sudo /home/yunhao/.espressif/python_env/idf5.5_py3.13_env/bin/python -m esp_idf_monitor \
  --port /dev/ttyACM0 --baud 115200 \
  /home/yunhao/github/stackchan/StackChan/firmware/build/stack-chan.elf
```

Optional: allow non-sudo serial access after next login:

```bash
sudo usermod -aG dialout yunhao
```

Then log out and log back in.
