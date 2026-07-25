# StackChan Nanobot GLM Phase 1 Implementation Record

Updated: 2026-07-24

## Implemented

- Domestic Zhipu adapters for `glm-asr-2512`, `glm-tts`, and `glm-4.6v-flash`.
- Nanobot Chat default `glm-4.7-flash`; optional DeepSeek Chat provider.
- Persistent Nanobot process and stable per-device session keys.
- Standard Streamable HTTP MCP server at `127.0.0.1:12801/mcp`.
- Device MCP `initialize`, paginated `tools/list`, JSON-RPC correlation, timeout, error, and disconnect handling.
- Per-turn local Tool Router with a maximum of 20 device schemas.
- Automatic, confirmation, and denied permission tiers.
- Camera upload endpoint and explicit confirmation gate.
- Native RTC/NVS multi-timer service with eight named timers.
- TIMER Mooncake App, AI nearest-timer badge, local full-screen expiry alert, and sound.
- `self.timer.start/list/pause/resume/cancel` tools and legacy reminder compatibility.
- `self.robot.dance` and `self.robot.stop_dance` tools.
- Portable mDNS device endpoint and provider-safe start/stop scripts.
- Server-side Opus/WebRTC VAD for Xiaozhi `auto` listening turns.
- Real-time TTS packet pacing to stay within the ESP32's 2400 ms decode queue.

## Automated Evidence

```text
tests/test_bridge_capabilities.py          passed
tests/test_timer_persistence.py            passed
scripts/test_stackchan_bridge_protocol.py  passed
scripts/test_stackchan_mcp_protocol.py     passed
scripts/test_stackchan_nanobot_tool_loop.py passed
```

The Nanobot tool-loop test uses a fake OpenAI-compatible GLM endpoint. The fake model requests `mcp_stackchan_self_timer_start`; Nanobot calls the local MCP server; the Bridge issues device `self.timer.start`; the result returns through Nanobot for the final response. The Bridge does not implement the model tool loop itself.

## Firmware Build

Command:

```bash
IDF_PATH=/path/to/esp-idf /path/to/idf-python /path/to/esp-idf/tools/idf.py build
```

Result:

```text
Generated StackChan/firmware/build/stack-chan.bin
Binary size: 0x3a1dc0
Smallest app partition: 0x4f0000
Free: 0x14e240 (26%)
```

## Flash

The phase-one build was flashed to the physical ESP32-S3 on 2026-07-24 with:

```bash
STACKCHAN_PORT=/dev/ttyACM0 ./scripts/flash_stackchan.sh
```

Observed evidence:

```text
Chip: ESP32-S3 revision v0.2
USB serial: /dev/ttyACM0
Application bytes written: 3,808,704 (0x3a1dc0)
Bootloader/application/partition/OTA/assets hashes: verified
Reset after flash: hard reset via RTS, followed by a physical Reset press
```

The flashed application size matches the build output above. This command did not
run an explicit whole-chip erase; application and asset partitions were replaced.
Normal firmware boot and application-level acceptance still require device logs and
runtime behavior evidence.

### Physical Boot Evidence

Normal boot was verified over `/dev/ttyACM0` after a short RST press:

```text
boot:0x2b (SPI_FAST_FLASH_BOOT)
Loaded app from partition at offset 0x20000
Project name: stack-chan
App version: 1.4.3
Compile time: Jul 23 2026 23:04:02
```

The physical device registered the new MCP tools:

```text
self.robot.dance
self.robot.stop_dance
self.timer.start
self.timer.list
self.timer.pause
self.timer.resume
self.timer.cancel
```

RTC/NVS initialization reported `restored 0 timer(s) from NVS`, and the launcher
created the new `TIMER` App. This verifies normal boot and initial timer restore,
but not yet restoration of an active timer after a reboot.

## Current External Blocker

The local GLM env is configured and remains ignored by Git with mode `0600`.
External checks on 2026-07-24 established:

```text
glm-4.7-flash: non-empty Chat response with thinking.type=disabled
glm-asr-2512: request accepted and response returned for a synthetic tone
glm-tts: valid audio payload returned (multiple runs, 240720+ bytes)
glm-4.6v-flash: test image identified as white on the first run
```

The Vision service later returned `429/1305` because the model was overloaded.
GLM media calls now use bounded retries for `429`, `5xx`, and network timeouts;
authentication and other deterministic failures are not retried. A successful
synthetic ASR request proves endpoint access only, not real speech transcription.

Physical-device evidence on 2026-07-24 now establishes OTA, WebSocket v3, real
Opus input, GLM-ASR transcription, a persistent Nanobot turn, MCP discovery of 25
device tools (20 model-visible), and a successful `self.timer.start` device call.
The command "设置一个10秒的倒计时" returned `isError:false` from the real device.
GLM-TTS generated reply Opus, but initial burst delivery overflowed the firmware's
2400 ms decode queue and truncated speech. The Bridge now paces each 60 ms packet;
complete physical playback remains to be re-verified after restart.

## Remaining Real-Device Acceptance

- Re-verify complete GLM-TTS playback with paced packets.
- Execute light, head movement, and timer through real model tool calls.
- Timer tool execution is proven; light and head movement remain.
- Reboot while a timer is active and verify RTC/NVS restoration.
- Verify camera is not used before explicit confirmation.

## 2026-07-24 Voice Transport And Subtitle Hardening

Real-device logs exposed a WebSocket lifecycle failure after the initial hello:

```text
[tts] generated opus packets=74
[ws] disconnected: Broken pipe
[http] code 400, Bad request version (<Opus binary>)
```

The Bridge synchronously synthesized and paced the spoken "bridge connected"
message inside the WebSocket receive thread. While that thread was blocked, the
device started sending listen/audio frames. After the socket failed,
`BaseHTTPRequestHandler` attempted to parse buffered WebSocket binary as another
HTTP request.

The implemented correction:

- Hello now sends a non-speaking screen status and immediately resumes WebSocket reads.
- Upgraded HTTP connections are explicitly marked for close after the WebSocket loop.
- Each connection has a stable physical-device key; a slow ASR/LLM turn delivers to
  the newest live connection for the same device after reconnect.
- Nanobot conversation keys now use the stable device key instead of a transient
  WebSocket session ID.
- VAD `no_speech_timeout` and empty ASR results silently return the device to idle;
  they no longer synthesize the confusing "ASR returned no text" message.
- Long replies are split into subtitle-sized utterances (default 42 characters),
  with TTS generated and paced one utterance at a time.
- Logs now include device/session IDs and elapsed time for ASR, Nanobot, TTS
  generation, TTS playback, reconnect migration, and session registration.

The StackChan avatar display also required firmware changes. Its custom speech
bubble ignored the `user` role and used a fixed single-line circular label. It now
shows user STT as well as assistant text and uses a three-line wrapped bubble. The
Bridge utterance limit is sized for this bubble.

Automated evidence after these changes:

```text
17 Bridge/media/router/timer unit tests passed
scripts/test_stackchan_bridge_protocol.py passed
Python module compilation passed
No Bad request version in the protocol regression log
```

Firmware build evidence:

```text
Generated StackChan/firmware/build/stack-chan.bin
Binary size: 0x439ba0
Smallest app partition: 0x4f0000
Free: 0xb6460 (14%)
```

This newer image includes the previously built timer/MCP/wake-word changes plus
the subtitle changes. It is built but not yet flashed at the time of this entry.

Network note: startup derives the advertised mDNS address from the active route;
it does not hardcode a LAN IP. At the end of this run Linux had only
`10.109.161.191/20`, while the last observed StackChan address was
`192.168.137.208`. Real-device acceptance must resume only after both endpoints
are on the same reachable network (or an explicit routed/forwarded deployment is
configured).

## 2026-07-25 Timer, Wake Word, And TTS Latency Repair

Real-device logs proved that `self.timer.start` returned a bare integer. For the
40-second timer this value was `6`, the timer ID. GLM interpreted it as six
remaining seconds. The firmware now returns an explicit object:

```json
{
  "id": 6,
  "name": "40秒倒计时",
  "duration_seconds": 40,
  "remaining_seconds": 40,
  "status": "started"
}
```

The timer-expiry reset was observed as a WebSocket disconnect followed by a new
OTA request. The expiry callback previously constructed a full-screen LVGL view
and demuxed the notification OGG synchronously inside the 4 KiB StackChan update
task. The repair moves UI and sound work to the Xiaozhi main task, increases the
update task stack to 8 KiB, and logs scheduling, rendering, sound queueing, and
both task stack high-water marks. This removes the identified unsafe execution
path, but the reset fix remains pending physical expiry verification.

Wake-word configuration is restored to Espressif AFE WakeNet9 with the trained
`Hi StackChan` model. The custom `Hey Judy`/MultiNet path is disabled. A detection
now reports `listen.detect` to the Bridge even when wake-word audio upload is
disabled, transitions an already-open channel through `Connecting`, and plays the
local listening popup after entering the listening state.

GLM-TTS now requests `stream=true`, `response_format=pcm`, and
`encode_format=base64`. Incoming 24 kHz PCM is encoded directly into 60 ms Opus
packets with `opuslib_next`; the Bridge forwards each length-prefixed packet as it
arrives. The complete-WAV/FFmpeg path remains available by setting both
`STACKCHAN_TTS_STREAMING=0` and `STACKCHAN_TTS_COMMAND_STREAMING=0`. Live provider
tests succeeded, but GLM first-PCM latency varied from about 3.6 to 9.3 seconds,
so remaining initial delay is provider/network variability rather than local
whole-audio buffering.

Verification for this revision:

```text
ESP-IDF 5.5.4 full build: passed
stack-chan.bin size: 0x3a2660 (26% app partition free)
stack-chan.bin SHA256: 00af84a91f41962832968f57460c32acbc141d211dc010d4d26a7781cf4f43d3
Bridge/router/media/timer tests: 20 passed
Bridge WebSocket protocol: passed
MCP pagination/proxy/permissions: passed
Nanobot-owned structured timer Tool Call loop: passed
Secret scan: passed
Real GLM streaming TTS request and Opus output: passed
```

Required physical acceptance after flashing this image:

1. Say `Hi StackChan` twice in separate idle turns; verify popup sound and two
   `[wake]` events.
2. Start a 40-second timer; verify MCP reports 40 remaining seconds and the reply
   never treats its ID as duration.
3. Let the timer expire; verify full-screen alert, local sound, and no reboot.
4. Reboot with a longer timer active; verify RTC/NVS restoration and expiry.

## 2026-07-25 Live Latency Diagnosis

One real 20-second timer turn took about 93 seconds end to end: 3.06 seconds
capture/VAD, 3.01 seconds GLM-ASR, 78.32 seconds Nanobot/GLM Chat across the
tool loop, and 8.86 seconds GLM-TTS plus paced playback. The device MCP call
completed within the approximately 344 ms tool iteration window. The timer
therefore expired before the model produced its spoken confirmation.

One real weather turn took about 186 seconds: 2.88 seconds capture/VAD, 6.71
seconds ASR, 133.28 seconds Chat retries ending in GLM error 1305, and 43.41
seconds synthesizing and playing the verbose provider error. Bridge/Nanobot
prompt construction itself took only about 21 ms.

Direct `curl --noproxy open.bigmodel.cn` measurements isolated the provider:

```text
glm-4.7-flash plain chat #1: HTTP 429 / code 1305, 2.783 s
glm-4.7-flash plain chat #2: HTTP 200, 19.044 s
glm-4.7-flash plain chat #3: no response before 50.003 s timeout
glm-4.7-flash head Tool Call: HTTP 200, valid call, 2.453 s
glm-asr-2512 weather WAV: HTTP 200, correct transcript, 1.684 s
glm-tts streaming: HTTP 200, first HTTP byte 1.620 s, total 4.482 s
```

The main unstable bottleneck is GLM Chat, followed by variable GLM-TTS. Network
DNS/TCP/TLS setup was below two seconds and cannot explain the 78-133 second
turns. Runtime configuration now maps `STACKCHAN_CHAT_TIMEOUT` to Nanobot's real
OpenAI-compatible timeout, honors `STACKCHAN_CHAT_MAX_RETRIES=0`, sanitizes
provider errors, and rejects digits-only ASR such as `10` instead of letting old
timer context reinterpret it as a new countdown. The interactive timeout is 15
seconds.

## 2026-07-25 Head Routing, Provider Busy, And Reset Diagnosis

The device inventory contained the real tools
`self.robot.get_head_angles` and `self.robot.set_head_angles`, but the router did
not classify phrases such as `向右转30度` or `向上转30度` as head movement. It
therefore exposed only `self.get_device_status` for those turns. GLM then
hallucinated the nonexistent historical alias `mcp_stackchan_self_head_move`.
The router now covers general directional language and unit tests cover right,
up, left, and down/head commands. Nanobot logs both the selected raw MCP names
and wrapped model names. The runtime session namespace moved to
`stackchan-phase1-v3` so invalid tool calls in the old history are not replayed.

Five direct non-thinking `glm-4.7-flash` calls with the real head schemas all
returned HTTP 200 and the correct `set_head_angles` call. Total times were
0.695, 0.673, 1.323, 0.690, and 0.879 seconds. This proves the model can perform
the action, while the earlier 1305 events remain a provider availability issue.
Provider placeholders such as `[Assistant reply unavailable due to model
error.]` are now converted to the short Chinese busy response instead of being
spoken verbatim.

DeepSeek support now defaults to the current `deepseek-v4-flash` model rather
than deprecated `deepseek-chat`. The hotspot launcher chooses a provider-aware
default and sends `thinking=disabled` for low-latency DeepSeek turns. DeepSeek
currently supplies Chat/Tool Calls but no documented ASR or TTS endpoint, so a
DeepSeek deployment keeps GLM ASR/TTS or replaces those stages locally.

Bridge logs showed a repeated reset signature: after an error TTS completed,
the next wake entered listening, emitted roughly three audio frames, and then
the device performed OTA/WS startup again. In listening mode the firmware was
feeding the microphone to both AFE WakeNet and the AFE voice processor. Idle
WakeNet already reports the wake event, so listening-mode WakeNet is now
disabled while idle wake-word detection remains enabled. The firmware also adds
`boot_info.boot_count`, numeric reset reason, and `reset_reason_name` to every
OTA request. This is a targeted likely fix plus telemetry; physical reproduction
is still required before closing the reset defect.

Local deployment assessment for this machine:

- Hardware: Core Ultra 9 285HX (24 cores), 30 GiB RAM. RTX 5070 Laptop is
  present but unavailable because the NVIDIA driver is not working.
- ASR first choice: quantized SenseVoice-Small ONNX/sherpa-onnx. The Bridge
  already owns VAD and utterance segmentation; ESP32 WakeNet remains KWS.
- Low-latency TTS first choice: a Chinese sherpa-onnx offline TTS model. For
  higher voice quality, evaluate CosyVoice 0.5B after GPU access is repaired.
- Local Agent first choice: Qwen3-4B GGUF Q4_K_M through `llama-server --jinja`
  with thinking disabled. Qwen3-8B is the accuracy candidate after measuring
  latency; Qwen3.5-4B is a secondary A/B candidate because its local tool-call
  stack is newer.

Verification for this revision:

```text
Bridge tests: 33 passed
ESP-IDF 5.5.4 full build: passed
stack-chan.bin size: 3812336 bytes (26% app partition free)
stack-chan.bin SHA256: cc738a88807fe97b64cdb2926731a969f21bf027003179a507d41b5a36afcd72
Repository secret scan excluding local .run data: clean
```

Required next physical test:

1. Flash this image and enter Agent mode.
2. Confirm the OTA log contains `boot_info`.
3. Say `你好小智，请向右转30度`; verify selected head tools, a real MCP result,
   and physical movement.
4. Trigger at least three separate wake/answer turns, including one provider
   error if it occurs; verify no reboot.
5. If it reboots, read `boot_info.reset_reason_name` from the following OTA body
   and preserve a serial trace for the panic backtrace.

## 2026-07-25 Agent Startup Fast Path

The original Xiaozhi activation sequence ran `CheckNewVersion()` synchronously
before constructing the protocol. A failed request retried up to ten times with
10, 20, 40 second exponential delays, so the screen remained on `Check new
version` even though a valid WebSocket endpoint was already stored in NVS.

The firmware now validates and loads the cached `websocket` NVS configuration
and initializes Agent mode immediately. It does not run an automatic OTA HTTP
refresh alongside the live WebSocket protocol. Firmware checks remain available
through the explicit system-update UI. The first run on a device without cached
WebSocket configuration retains the bootstrap OTA path.

Readiness text is now precise:

```text
Nanobot 已就绪 = WebSocket hello plus MCP initialize and paged tools/list succeeded
```

Bridge logs include OTA response `elapsed_ms` and MCP discovery `ready_ms`.
`Nanobot 已就绪` is the user-visible signal that voice-controlled device tools
are ready. The intermediate Bridge-only text was removed to avoid presenting a
transport connection as full readiness.

The first physical fast-path image exposed a panic after a failed mDNS lookup.
The captured backtrace resolved exactly to `Application::ActivationTask()` at
the background refresh timing log. Newlib nano printf did not safely consume
the `%lld` argument: the preceding log printed `in ld ms` and a garbage boolean,
then the following `%s` interpreted the shifted integer argument as a pointer,
causing `LoadStoreError`. Activation timings now use an explicit 32-bit
millisecond value and `%d`, and a source regression test prohibits `%lld` in
this path. Removing the unnecessary concurrent refresh also removes the dual
HTTP/WebSocket mDNS lookup that preceded the failure.

The same serial trace measured Agent startup: cached activation itself took
about 240 ms, while Wi-Fi needed about 27 seconds. The first scan missed the AP,
waited the default 10 seconds, then associated after three retries at -81 dBm.
StackChan now configures scan retry backoff as 1, 2, 4 seconds up to 30 seconds.
Weak Wi-Fi can still dominate startup and must be treated separately from OTA,
MCP, and model latency.

Native StackChan apps and Agent mode use the same `WifiManager` singleton, but
the Agent startup path previously called `TryWifiConnect()` unconditionally.
It now checks the singleton after installing the Agent network callback. If the
launcher is already online, it publishes the existing SSID as a connected event
and reuses the current interface/IP without scanning. Scan retry is only used
when the device is actually offline.

Latest fast-path firmware artifact:

```text
Path: StackChan/firmware/build/stack-chan.bin
Size: 3813552 bytes (26% app partition free)
SHA256: 23ad54b259bfd29f9e263853ce7cdbb1dcc79cf313be8e5270c0860ce636d96f
Bridge and firmware contract tests: 37 passed
ESP-IDF 5.5.4 full build: passed
```

## 2026-07-25 Wake Interruption And Turn Timing

The local wake gate originally armed only on `listen/state=detect`. Xiaozhi uses
a second valid sequence when the wake word interrupts speaking or listening:
`abort/reason=wake_word_detected` followed by `listen/state=start`. The Bridge
did not arm that path, so it logged incoming Opus while every frame remained at
`buffered=0`. Both protocol sequences now call the same `arm_wake()` state
transition. Tests reproduce the exact abort/start/audio sequence.

The Tool Router also now classifies the Chinese noun `舞蹈` as the dance group.
Previously `来一段舞蹈` exposed only device status, allowing conversation
history to suggest an unavailable head tool instead of the real
`self.robot.dance` and `self.robot.stop_dance` tools.

Measured timing for a failed `设置一个20秒倒计时` turn:

```text
Capture and VAD endpoint: 3720 ms (includes about 780 ms end silence)
GLM ASR:                 1022 ms, transcript correct
Nanobot context build:     25 ms
GLM Chat:               15325 ms, request timeout
Device MCP Tool Call:       0 ms, no Tool Call was produced
GLM TTS first packet:    1165 ms
TTS complete playback:   5306 ms for 68 paced Opus packets
```

The blocking failure was GLM Chat. ASR was healthy. Most time after the TTS
first packet was expected real-time audio pacing, not synthesis startup. English
timeout placeholders are now sanitized to a short Chinese provider-busy reply.

## 2026-07-26 Agent Status, Continuous TTS, And Tool Allowlist

The StackChan display override had an empty `ShowNotification()` method. Network
scan, association, and connected events were emitted correctly, but none could
reach the avatar speech bubble. The override now renders every non-empty
notification in the avatar bubble. The Bridge also sends a visible transport
message immediately after WebSocket hello, replaces it with `Nanobot 已就绪`
after MCP discovery, and displays an explicit MCP loading failure when discovery
times out.

A 150-character answer was split into five subtitle/TTS segments. Each segment
started a separate GLM-TTS process and cloud request. Measured first-packet gaps
were 0.8-1.3 seconds per segment and the complete response took 37.6 seconds.
Streaming mode now sends the normalized full answer through one progressive TTS
request, while the avatar wraps the complete subtitle. The stop grace is 300 ms.
Each device session also owns a TTS generation token: a new response, abort,
wake interruption, disconnect, or session replacement invalidates the previous
stream so stale audio cannot continue into the next turn.

The Tool Router previously added `self.get_device_status` to every turn. A turn
with no relevant intent therefore always exposed a device tool. In addition,
Nanobot internally evaluates `tools or self.tools`; a standard empty registry is
falsey and can accidentally fall back to the global registry. The turn registry
is now explicitly truthy even when empty, so an empty local allowlist remains
empty. Device status is available only for explicit status intent. Absolute
wall-clock reminder requests are withheld because the current firmware exposes
duration-based countdown/reminder tools, not calendar or todo storage.

The log entry `Tool call: mcp_stackchan_self_timer_list({})` for `八点吃晚饭`
was a model-generated call to a real globally known name but not a turn-approved
tool. It produced no device `tools/call`, and `tools_used=[]` confirms it was not
executed. Rejected names are now logged as `rejected_unavailable` with the exact
per-turn allowlist. The session namespace moved to `stackchan-phase1-v4` to avoid
replaying contaminated phase1-v3 history.

Verification for this revision:

```text
Bridge and firmware contract tests: 48 passed
Python compile checks: passed
ESP-IDF 5.5.4 full build: passed
stack-chan.bin size: 3813856 bytes (26% app partition free)
stack-chan.bin SHA256: 34899df727a77a45812e7d7ff4be7ffac90822e9039604ab28083ea1f2793304
```

Physical verification still required: flash this image, enter Agent mode, verify
visible connection transitions, run a long spoken answer without segment gaps,
and confirm an unsupported memory/todo request executes no MCP call.

## 2026-07-26 First-Connection MCP Warm-up

The first physical run showed an MCP `initialize` response arriving just after
the Bridge's fixed 10-second deadline. The pending request had already been
removed, so the valid response was logged as `unhandled` and the UI incorrectly
asked the user to exit. Re-entering Agent mode completed the same discovery in
392 ms and returned all 25 tools, proving this was a warm-up timeout rather than
an incompatible device.

MCP initialize now uses a configurable 15-second window and retries once on the
same live connection. The UI reports an automatic retry instead of requiring an
exit. RPC logs include request ID, method, timeout, and elapsed time. Messages
whose embedded `session_id` belongs to the replaced WebSocket are rejected; the
captured run contained two stale wake/listen messages from the previous session.
After a successful WebSocket open, firmware resets the decoder/playback queues
and clears the previous network error so an old connection-failure sound cannot
play during the new listening turn.

Verification for this revision:

```text
Tests: 51 passed
ESP-IDF 5.5.4 full build: passed
stack-chan.bin size: 3813872 bytes (26% app partition free)
stack-chan.bin SHA256: 01cb39937f41d77e46eac99ef1571bed0a7e7fef6945c76f2f6cdcca68433dd3
```

The captured wake turn stopped after only three Opus packets and produced no
disconnect or reset record in the Bridge log. That is insufficient to assign a
root cause. Re-test after the stale-session and warm-up fixes; if it repeats,
capture firmware serial state transitions while reproducing it.

## 2026-07-26 Tool Protocol And Timer Panic Repair

Physical logs exposed model text that looked like a tool call but was not a
structured provider tool call:

```text
<tool_call>mcp_stackchan_self_robot_get_head_angles</tool_call>
<tool_call>cron ... </arg_key> ...
```

Nanobot reported `tools_used=[]`, proving these strings were never executed.
They were ordinary model output and the Bridge incorrectly forwarded them to
the avatar and GLM-TTS. The Bridge now rejects tool/function/argument markup in
both the Nanobot result boundary and the final device-output boundary. The user
receives a short retry message, and logs retain the rejected content for
diagnosis without sending it to the device.

The lexical per-turn router also returned an empty tool set for natural phrases
such as `跳个舞`. The current firmware has exactly 20 policy-approved tools, so
the Bridge now supplies the complete bounded safe inventory on each turn. The
permission layer still removes reboot, firmware, network, asset, and raw screen
operations. Only `web_search` and `web_fetch` are retained from Nanobot's native
tools; `exec`, file mutation, spawn, and cron remain unavailable to voice turns.
Inventories larger than 20 still use bounded intent routing.

Camera confirmation previously reused the generic 20-second device RPC timeout.
The camera path includes capture, JPEG upload, and GLM vision analysis, so a
valid request timed out before the device could return. It now uses a separate
`STACKCHAN_CAMERA_MCP_TIMEOUT` (default 120 seconds); ordinary tools retain
`STACKCHAN_MCP_RPC_TIMEOUT` (default 20 seconds).

The observed timer expiry was a real firmware panic, not an intentional Agent
exit. The previous callback scheduled a full-screen Avatar decorator on the
Xiaozhi application task while the StackChan update task concurrently iterated
the same decorator pool. The callback now copies notification data into a
bounded queue, and the StackChan owner task alone creates and updates the
decorator. Flash ELF coredumps are enabled in the existing 64 KiB coredump
partition so any remaining physical panic can be decoded instead of inferred
from the following boot reason.

Verification for this revision:

```text
Tests: 61 passed
ESP-IDF 5.5.4 full build: passed
stack-chan.bin size: 3829696 bytes (26% app partition free)
stack-chan.bin SHA256: 4d58d78a4e59f47087e5ba119113825afafc6f814b85533fe230c303729717d6
```

Physical verification is still required. Flash this exact image, then test
`跳个舞`, a 20-second timer through expiry, a normal Web query, and a confirmed
camera request. The timer test must show the full-screen alert without a new OTA
request whose `boot_info.reset_reason_name` is `panic`.

## 2026-07-26 Vision Completion And TTS Watchdog

A confirmed camera request produced a valid GLM Vision answer and the Bridge
returned HTTP 200, but a new wake event arrived before the device completed its
camera MCP response. The device then reported `Failed to upload photo`, so the
successful description never reached TTS. This was not a non-streaming TTS
failure: nearby replies consistently logged first Opus packets in 0.8-1.4
seconds and completed with `streaming=true`.

The Bridge now caches a successful Vision result by physical device for the
duration of the confirmed operation. If device-side HTTP completion races with
a wake event, the Bridge recovers only a result produced after that operation
started and streams the description directly, without a second GLM Chat call.
Markdown emphasis is removed before speech. The TTS subprocess reader now uses
non-blocking polling with configurable first-packet and stream-idle watchdogs,
so provider stalls can be cancelled instead of blocking for the full HTTP
timeout.

Logs around the reported Agent exit showed no WebSocket disconnect, no new OTA
request, and no panic. Health remained `ws_clients=1`, `connected=true`, with 25
device tools. The final sequence was a complete 57-packet TTS response followed
by wake detection and `listen/start`; this is an idle/listening UI transition,
not a device reboot or transport exit.

Verification for this Bridge-only revision:

```text
Tests: 63 passed
Python compile checks: passed
```
