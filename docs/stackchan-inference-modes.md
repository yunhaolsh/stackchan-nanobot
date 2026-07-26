# StackChan Inference Modes

The Bridge supports three explicit inference modes without changing firmware:

| Mode | Chat | ASR / TTS | Vision | Cloud key required |
| --- | --- | --- | --- | --- |
| `cloud` | configured cloud provider | configured cloud providers | configured cloud provider | yes |
| `local` | local `llama-server` | local `sherpa-onnx` service | optional local VLM | no |
| `hybrid` | cloud, then Nanobot fallback | cloud, then local wrapper | cloud, then local when enabled | yes |

The ESP32 still handles wake word, audio capture, Opus transport, display,
servos, LEDs, camera, timers, and MCP Tools. Inference runs on the Bridge host.
The text LLM does not need to be multimodal: the camera path can use a separate
VLM, which keeps normal Tool calls faster and lowers memory use.

Local mode uses a versioned mode-specific session namespace, a 60-second LLM
request deadline, and an 8-message replay window by default. The local context
window is 8K tokens and voice replies are capped at 256 output tokens. This
prevents a long cloud conversation from filling local context and keeps stale
tool results out of a new interaction-policy version. These limits can be
changed with `STACKCHAN_LOCAL_CHAT_TIMEOUT_S`,
`STACKCHAN_LOCAL_MAX_MESSAGES`, `STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS`, and
`STACKCHAN_LOCAL_CHAT_MAX_TOKENS`.

Every turn passes through the local Tool Router before Nanobot calls the model.
Only the prompt-relevant, policy-approved operation is included. For example,
creating a countdown exposes `self.timer.start`, not all timer operations;
ordinary conversation receives no device Tool schemas. This bounds prompt size
for both the current 20-Tool firmware and future larger capability inventories.

The Bridge rejects action-success text unless Nanobot reports a real Tool result.
Absolute alarms, calendar/todo storage, shutdown, reboot, firmware upgrades, and
network reconfiguration are not implemented voice capabilities and are reported
as such instead of being simulated by the model. Camera routing requires explicit
visual language such as taking a photo or describing the current view; the verb
"查看" alone never selects Vision.

## Local Baseline

- Chat and Tool calls: `Qwen/Qwen3-4B-GGUF`, Q4_K_M, served by `llama.cpp`.
- ASR: `SenseVoiceSmall` int8 through `sherpa-onnx`.
- TTS: `vits-melo-tts-zh_en` through `sherpa-onnx`.
- Vision: standard OpenAI-compatible adapter for a separately hosted
  `Qwen3-VL-4B-Instruct`; disabled by default in the first local baseline.

The setup scripts pin Python packages, model archive URLs, checksums, and a
llama.cpp release. Runtime data is stored under ignored paths: `.venv-local-speech/`,
`.local-runtime/`, `models/`, and `.run/`.

## Repository Boundaries

`/home/yunhao/github/stackchan` is the host-service repository and pushes to
`yunhaolsh/stackchan-nanobot`. Its nested `StackChan/` directory is an independent
firmware Git repository that pushes to `yunhaolsh/StackChan`; firmware commits are
not mixed with Bridge commits. Generated runtime files, filled env files, model
weights, virtual environments, and local llama.cpp builds stay outside both
repositories through `.gitignore`.

## Install Local Services

Required host tools are Python 3.10-3.12, `ffmpeg`, `git`, `cmake`, a C++ compiler,
`curl`, and `tar`.

```bash
cd stackchan-nanobot
./scripts/setup_stackchan_local_speech.sh
STACKCHAN_LLAMA_GPU_BACKEND=cpu ./scripts/setup_stackchan_local_llm.sh
```

Use `STACKCHAN_LLAMA_GPU_BACKEND=cuda` on a host with a working CUDA toolkit.
The default CPU build is the portable baseline for Linux and WSL.

Start the persistent model services, then start the Bridge in local mode:

```bash
./scripts/start_stackchan_local_inference.sh
STACKCHAN_INFERENCE_MODE=local \
  STACKCHAN_ENV_FILE="$PWD/.run/stackchan-nanobot.env" \
  ./scripts/start_stackchan_nanobot_hotspot.sh
```

The LLM setup downloads the 2.5 GB GGUF with resume support and verifies its
SHA-256 before making it available. The local startup command never downloads
models silently and waits for both health endpoints.

```bash
curl --noproxy '*' http://127.0.0.1:18080/health
curl --noproxy '*' http://127.0.0.1:18081/health
curl --noproxy '*' http://127.0.0.1:12800/health
./scripts/check_stackchan_local_inference.py
./scripts/check_stackchan_local_nanobot.py
```

The Bridge health response reports `inference_mode`, `chat_model`,
`chat_timeout_s`, and `session_namespace`. Use it to verify that a running
process actually picked up local settings:

```bash
curl --noproxy '*' http://127.0.0.1:12800/health | jq '{
  inference_mode, chat_model, chat_timeout_s, session_namespace
}'
```

Stop the services with:

```bash
./scripts/stop_stackchan_nanobot.sh
./scripts/stop_stackchan_local_inference.sh
```

## Cloud And Hybrid

`cloud` preserves the existing GLM/DeepSeek and GLM ASR/TTS/Vision paths. Keep
provider keys only in `.run/stackchan-nanobot.env`.

For `hybrid`, set `STACKCHAN_INFERENCE_MODE=hybrid`. Nanobot's native
`fallbackModels` owns Chat fallback. It only changes model after a transient
provider failure; Tool execution remains in Nanobot and the device MCP path.
The ASR/TTS/Vision wrapper invokes a local provider only if the cloud command
fails before producing output. This avoids duplicate TTS audio. Device Tools
are not retried by the provider wrapper.

## Migration And Security

The StackChan device only needs the Bridge WebSocket URL. ASR, TTS, LLM, and
VLM endpoints are host-side configuration, so changing hosts does not require
flashing firmware. For another Linux or WSL machine:

1. Clone with submodules.
2. Recreate `.venv-nanobot` and the local speech environment.
3. Run the two local setup scripts.
4. Create an ignored `.run/stackchan-nanobot.env` from the example.
5. Start local inference and then the Bridge.

Local inference services bind to `127.0.0.1` by default. Keep them loopback-only
when they share a host with the Bridge. For a remote inference host, use TLS or
a private network and set component-specific tokens; never expose an unauthenticated
OpenAI-compatible endpoint to the public Internet. API keys are referenced as
environment placeholders in generated Nanobot config and are never written into
the repository runtime template.

## Verified Baseline

The CPU-only baseline was exercised on 2026-07-26 with a Lenovo host, an Intel
Core Ultra 9 285HX, and about 31 GB of available RAM:

- Local Chat health: 5 ms; short cached reply: 409 ms.
- Direct structured timer Tool Call: 1.264 s.
- Local MeloTTS: 428 ms for 233,516 bytes of WAV audio.
- SenseVoice transcription of that generated sentence: 51 ms.
- Nanobot with five registered MCP capabilities routed only `self.timer.start`,
  called the simulated device once with `duration_seconds=20`, and completed the
  two-iteration Tool loop in 9.77 s with the final policy prompt.
- The compact voice prompt reduced the first Tool iteration from 2,091 to 487
  prompt tokens. The earlier unoptimized Nanobot round trip was 29.1 s.

After the interaction-path optimization on the same host, direct warm-process
measurements were: short local reply 729 ms, structured timer Tool Call 3.17 s,
local TTS generation 445 ms, and local ASR inference 52 ms. Physical playback
still runs at real-time audio duration and should not be confused with synthesis
latency.

These are warm-process measurements, not a physical-device local-mode result.
Real StackChan audio transport, playback, and device Tool execution still need
to be validated after starting the Bridge in `local` mode. Local Vision remains
optional and was not included in this baseline.
