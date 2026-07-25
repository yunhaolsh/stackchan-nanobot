# StackChan Nanobot Bridge

This runtime connects StackChan firmware to a persistent Nanobot agent. Nanobot owns conversation history, model calls, skills, and tool-call iteration. The Bridge owns the Xiaozhi transport, media conversion, device MCP proxy, and local permission policy.

## Data Path

```text
StackChan microphone
 -> Xiaozhi WebSocket v3 Opus
 -> Bridge
 -> GLM-ASR-2512
 -> Nanobot persistent session
 -> GLM-4.7-Flash or DeepSeek Chat
 -> Nanobot MCP client
 -> Bridge local MCP server
 -> StackChan MCP tools/call
 -> Nanobot final answer
 -> GLM-TTS
 -> Opus over WebSocket
 -> StackChan speaker
```

The ESP32-S3 does not run Nanobot. It runs the device firmware and local capabilities. The Bridge can run on a PC, NAS, or server reachable from the device.

## Defaults

| Capability | Provider/model |
|---|---|
| Chat and tool calls | Zhipu `glm-4.7-flash` |
| ASR | Zhipu `glm-asr-2512` |
| TTS | Zhipu `glm-tts` |
| Vision | Zhipu `glm-4.6v-flash` |
| Optional Chat switch | DeepSeek `deepseek-v4-flash` |

Chat can use DeepSeek while ASR, TTS, and Vision continue to use Zhipu.

## Install

Requirements:

- Python 3.11 or later
- `ffmpeg` with `libopus`
- Python `opuslib-next` and `webrtcvad-wheels` (installed by `requirements.txt`)
- mDNS support on the host network
- ESP-IDF v5.5.4 only when building firmware

```bash
cd /path/to/stackchan
python3 -m venv .venv-nanobot
.venv-nanobot/bin/python -m pip install -r requirements.txt
mkdir -p .run
cp nanobot_config/stackchan-nanobot.env.example .run/stackchan-nanobot.env
```

Edit only `.run/stackchan-nanobot.env` and set:

```bash
ZHIPU_API_KEY=your-local-key
```

The filled env file is local-only. Keys are never written to firmware, committed config, health output, or logs.

## Run

```bash
./scripts/start_stackchan_nanobot_hotspot.sh
```

The default device-facing hostname is `stackchan-nanobot.local`. The helper publishes it to the current LAN address, so changing Wi-Fi does not require a firmware rebuild. Use a direct IP only as a fallback:

```bash
STACKCHAN_PUBLIC_HOST=192.0.2.10 ./scripts/start_stackchan_nanobot.sh
```

On WSL or a multi-NIC host, keep the portable hostname and select the hotspot/LAN interface explicitly:

```bash
STACKCHAN_MDNS_ADDRESS=192.0.2.10 ./scripts/start_stackchan_nanobot_hotspot.sh
```

Stop:

```bash
./scripts/stop_stackchan_nanobot.sh
```

The Nanobot OpenAI-compatible debug API is disabled by default because the Bridge embeds the persistent Nanobot runtime. Enable it only for diagnostics:

```bash
STACKCHAN_ENABLE_NANOBOT_API=1 ./scripts/start_stackchan_nanobot.sh
```

## DeepSeek Chat

Set the following in the local env file:

```bash
STACKCHAN_CHAT_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-local-key
STACKCHAN_CHAT_MODEL=deepseek-v4-flash
```

Keep the Zhipu key for ASR, TTS, and Vision.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Bridge, device, media, and MCP status |
| `POST /xiaozhi/ota/` | Xiaozhi protocol bootstrap |
| `GET /ws` | Device WebSocket v3 |
| `GET /device/tools` | Discovered tools and local permission tier |
| `GET /permissions/pending` | Pending camera confirmations |
| `POST /permissions/{id}/confirm` | Confirm one pending action |
| `POST /permissions/{id}/cancel` | Cancel one pending action |
| `POST /vision/explain` | Authenticated device camera upload |
| `POST /nanobot/message` | Text diagnostic through Nanobot |
| `http://127.0.0.1:12801/mcp` | Local Streamable HTTP MCP server |

## Device Discovery

After WebSocket hello the Bridge:

1. Sends MCP `initialize` with the local Vision URL.
2. Calls `tools/list` until `nextCursor` is empty.
3. Caches the current schemas and online generation.
4. Reloads Nanobot's MCP connection when device capabilities change.
5. Correlates each `tools/call` response by JSON-RPC ID with timeout and disconnect handling.

The WebSocket receive loop never runs ASR or Nanobot synchronously. Audio turns run in workers, allowing MCP results to arrive while Nanobot is waiting on a tool call.

In firmware `auto` listening mode, StackChan keeps sending audio and does not send `listen.stop`. The Bridge decodes the 60 ms Opus packets, applies WebRTC VAD, and closes the turn after the configured end silence. TTS packets are sent at the device frame rate because the ESP32 decode queue holds only 2400 ms; burst delivery truncates longer replies.

Long replies are synthesized as subtitle-sized utterances. The default
`STACKCHAN_TTS_SEGMENT_MAX_CHARS=42` matches the custom firmware's three-line
avatar bubble. Empty ASR results return to idle without speaking an error.
For GLM-TTS, the adapter requests streaming 24 kHz PCM and continuously
transcodes it to the device's 16 kHz Opus framing. Set
`STACKCHAN_TTS_STREAMING=0` and `STACKCHAN_TTS_COMMAND_STREAMING=0` together to
fall back to complete-WAV generation.

Runtime logs identify the physical device and transient session, then report
elapsed time for ASR, Nanobot, TTS generation, packet playback, and reconnect
migration. They never include provider API keys.

## Permission Policy

- Automatic: status, light, head, volume, brightness, theme, timers, and dance.
- Confirmation required: `self.camera.take_photo`.
- Not exposed to the model: reboot, firmware upgrade, network/Wi-Fi changes, asset URL changes, screen upload, and arbitrary image URL preview.

The model requests an action; the Bridge permission layer authorizes it; firmware performs final range and state validation.

## Firmware Tools

The custom firmware adds:

```text
self.timer.start
self.timer.list
self.timer.pause
self.timer.resume
self.timer.cancel
self.robot.dance
self.robot.stop_dance
```

Timers use RTC absolute deadlines and eight NVS slots. The desktop includes a TIMER App, the AI avatar shows the nearest timer badge, and expiry uses a full-screen local alarm. Existing reminder tools remain available.

## Verify

Offline verification does not call external model providers:

```bash
./scripts/verify_stackchan_nanobot_local.sh
```

Individual checks:

```bash
.venv-nanobot/bin/python -m pytest -q tests/test_audio_endpoint.py tests/test_tts_streaming.py tests/test_bridge_capabilities.py tests/test_timer_persistence.py
.venv-nanobot/bin/python scripts/test_stackchan_bridge_protocol.py
.venv-nanobot/bin/python scripts/test_stackchan_mcp_protocol.py
.venv-nanobot/bin/python scripts/test_stackchan_nanobot_tool_loop.py
```

External provider verification sends generated test media and prompts to the configured services:

```bash
set -a
source .run/stackchan-nanobot.env
set +a
.venv-nanobot/bin/python scripts/check_stackchan_nanobot_runtime.py --external
```

## Build And Flash

Firmware config uses the portable mDNS host:

```text
CONFIG_OTA_URL="http://stackchan-nanobot.local:12800/xiaozhi/ota/"
CONFIG_STACKCHAN_SERVER_URL="http://stackchan-nanobot.local:12800"
CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES=y
```

Build:

```bash
. /path/to/esp-idf/export.sh
cd StackChan/firmware
idf.py build
```

With StackChan in download mode at `/dev/ttyACM0`:

```bash
cd /path/to/stackchan
STACKCHAN_PORT=/dev/ttyACM0 ./scripts/flash_stackchan.sh
```

Flashing is required for firmware/App/tool changes. Provider keys, models, prompts, Bridge code, and LAN address changes do not require another flash.

## Live Acceptance

After flashing, enter AI.AGENT and inspect:

```bash
tail -f .run/stackchan-bridge.log
```

Completion requires real evidence for ASR transcription, Nanobot reply/tool call, device MCP result, light, head movement, timer, TTS playback, and timer restoration after a device reboot. Offline tests and firmware build alone do not satisfy this acceptance gate.
