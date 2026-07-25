#!/usr/bin/env python3
import argparse
import base64
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]


def _ok(name: str, detail: str = ""):
    print(f"OK   {name}{': ' + detail if detail else ''}", flush=True)


def _fail(name: str, detail: str):
    print(f"FAIL {name}: {detail}", flush=True)


def _warn(name: str, detail: str):
    print(f"WARN {name}: {detail}", flush=True)


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_test_wav(path: Path):
    sample_rate = 16000
    duration_sec = 0.35
    frames = int(sample_rate * duration_sec)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for i in range(frames):
            sample = int(9000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            wav.writeframes(struct.pack("<h", sample))


def _check_files(config_path: Path) -> bool:
    ok = True
    required = [
        config_path,
        ROOT / "nanobot_bridge" / "audio_endpoint.py",
        ROOT / "nanobot_bridge" / "server.py",
        ROOT / "nanobot_bridge" / "capabilities.py",
        ROOT / "nanobot_bridge" / "mcp_http.py",
        ROOT / "nanobot_bridge" / "nanobot_runtime.py",
        ROOT / "scripts" / "start_stackchan_nanobot.sh",
        ROOT / "scripts" / "stackchan_asr_openai.py",
        ROOT / "scripts" / "stackchan_asr_gemini.py",
        ROOT / "scripts" / "stackchan_tts_openai.py",
        ROOT / "scripts" / "stackchan_tts_gemini.py",
        ROOT / "scripts" / "stackchan_audio.py",
        ROOT / "scripts" / "stackchan_glm_http.py",
        ROOT / "scripts" / "stackchan_asr_glm.py",
        ROOT / "scripts" / "stackchan_tts_glm.py",
        ROOT / "scripts" / "stackchan_vision_glm.py",
    ]
    for path in required:
        if path.exists():
            _ok("file", str(path))
        else:
            _fail("file", f"missing {path}")
            ok = False
    return ok


def _check_commands() -> bool:
    ok = True
    for cmd in ("ffmpeg", "curl"):
        result = subprocess.run(["bash", "-lc", f"command -v {cmd}"], capture_output=True, text=True)
        if result.returncode == 0:
            _ok("command", f"{cmd} -> {result.stdout.strip()}")
        else:
            _fail("command", f"{cmd} not found")
            ok = False
    return ok


def _check_python_imports() -> bool:
    ok = True
    for module in ("openai", "websockets", "nanobot", "opuslib_next", "webrtcvad"):
        try:
            __import__(module)
            _ok("python import", module)
        except Exception as exc:
            _fail("python import", f"{module}: {exc}")
            ok = False
    return ok


def _check_env(config: dict) -> bool:
    ok = True
    provider = config.get("providers", {}).get("openai", {})
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
        if os.environ.get(key):
            _ok("env", key)
        elif f"${{{key}}}" in json.dumps(provider):
            _fail("env", f"{key} is required by nanobot_config/config.json")
            ok = False
        else:
            _warn("env", f"{key} not set")

    openai_base_url = os.environ.get("OPENAI_BASE_URL", "")
    if "anyrouter.top" in openai_base_url:
        _warn("chat base url", "AnyRouter docs currently recommend https://anyrouter.dev/api/v1")
    if "anyrouter.dev" in openai_base_url and not os.environ.get("OPENAI_API_KEY", "").startswith("sk-ar-"):
        _warn("chat api key", "AnyRouter dev endpoint expects an sk-ar-* API key in OPENAI_API_KEY")

    model = _chat_model(config)
    if model:
        _ok("nanobot chat model", model)
    else:
        _fail("nanobot chat model", "missing modelPresets.primary.model")
        ok = False

    _ok("chat provider", os.environ.get("STACKCHAN_CHAT_PROVIDER", "glm"))
    _ok("asr model", os.environ.get("STACKCHAN_ASR_MODEL", "glm-asr-2512"))
    if os.environ.get("STACKCHAN_ASR_PROVIDER"):
        _ok("asr provider", os.environ["STACKCHAN_ASR_PROVIDER"])
    if os.environ.get("STACKCHAN_ASR_BASE_URL"):
        _ok("asr base url", "STACKCHAN_ASR_BASE_URL")
    else:
        _ok("asr base url", "STACKCHAN_GLM_BASE_URL default")
        if "anyrouter" in openai_base_url:
            _warn("asr base url", "AnyRouter chat endpoints may not support /audio/transcriptions; set STACKCHAN_ASR_BASE_URL")
    if os.environ.get("STACKCHAN_ASR_API_KEY"):
        _ok("asr api key", "STACKCHAN_ASR_API_KEY")
    else:
        _ok("asr api key", "ZHIPU_API_KEY/GLM_API_KEY fallback")
    _ok("tts model", os.environ.get("STACKCHAN_TTS_MODEL", "glm-tts"))
    if os.environ.get("STACKCHAN_TTS_PROVIDER"):
        _ok("tts provider", os.environ["STACKCHAN_TTS_PROVIDER"])
    if os.environ.get("STACKCHAN_TTS_BASE_URL"):
        _ok("tts base url", "STACKCHAN_TTS_BASE_URL")
    else:
        _ok("tts base url", "STACKCHAN_GLM_BASE_URL default")
        if "anyrouter" in openai_base_url:
            _warn("tts base url", "AnyRouter chat endpoints may not support /audio/speech; set STACKCHAN_TTS_BASE_URL")
    if os.environ.get("STACKCHAN_TTS_API_KEY"):
        _ok("tts api key", "STACKCHAN_TTS_API_KEY")
    else:
        _ok("tts api key", "ZHIPU_API_KEY/GLM_API_KEY fallback")
    _ok("tts voice", os.environ.get("STACKCHAN_TTS_VOICE", "tongtong"))
    _ok("vision model", os.environ.get("STACKCHAN_VISION_MODEL", "glm-4.6v-flash"))
    return ok


def _configure_chat_provider_env() -> None:
    provider = os.environ.get("STACKCHAN_CHAT_PROVIDER", "glm")
    if provider == "glm":
        key = (
            os.environ.get("STACKCHAN_CHAT_API_KEY")
            or os.environ.get("ZHIPU_API_KEY")
            or os.environ.get("GLM_API_KEY")
        )
        os.environ["OPENAI_API_KEY"] = key or ""
        os.environ["OPENAI_BASE_URL"] = os.environ.get(
            "STACKCHAN_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
        )
    elif provider == "deepseek":
        key = os.environ.get("STACKCHAN_CHAT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        os.environ["OPENAI_API_KEY"] = key or ""
        os.environ["OPENAI_BASE_URL"] = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def _configure_provider_proxy_bypass() -> None:
    if os.environ.get("STACKCHAN_BYPASS_PROVIDER_PROXY", "0") != "1":
        return
    hosts: set[str] = set()
    for variable in ("OPENAI_BASE_URL", "STACKCHAN_GLM_BASE_URL"):
        hostname = urlparse(os.environ.get(variable, "")).hostname
        if hostname:
            hosts.add(hostname)
    for variable in ("NO_PROXY", "no_proxy"):
        entries = [item.strip() for item in os.environ.get(variable, "").split(",") if item.strip()]
        for hostname in sorted(hosts):
            if hostname not in entries:
                entries.append(hostname)
        os.environ[variable] = ",".join(entries)


def _chat_model(config: dict) -> str:
    return os.environ.get("STACKCHAN_CHAT_MODEL") or config.get("modelPresets", {}).get("primary", {}).get("model", "")


def _chat_model_candidates(config: dict, raw: str) -> list[str]:
    candidates = [item.strip() for item in raw.split(",") if item.strip()]
    if candidates:
        return candidates
    return [_chat_model(config)]


def _external_chat(config: dict, candidates: list[str]) -> bool:
    try:
        from openai import OpenAI
    except Exception as exc:
        _fail("external chat", f"openai import failed: {exc}")
        return False

    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        timeout=float(os.environ.get("STACKCHAN_CHAT_TIMEOUT", "45")),
        max_retries=int(os.environ.get("STACKCHAN_CHAT_MAX_RETRIES", "0")),
    )
    for model in candidates:
        try:
            extra_body = None
            if os.environ.get("STACKCHAN_CHAT_PROVIDER", "glm") == "glm":
                extra_body = {
                    "thinking": {
                        "type": os.environ.get("STACKCHAN_CHAT_THINKING", "disabled")
                    }
                }
            result = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "用两个字回复：正常"}],
                max_tokens=64,
                extra_body=extra_body,
            )
            response_text = result.choices[0].message.content or ""
            if not response_text.strip():
                raise RuntimeError("response content was empty")
            _ok("external chat", f"{model} -> {response_text[:40]}")
            print(f"SUGGEST STACKCHAN_CHAT_MODEL={model}")
            return True
        except Exception as exc:
            _fail("external chat", f"{model}: {exc}")
    return False


def _is_gemini_asr() -> bool:
    return os.environ.get("STACKCHAN_ASR_PROVIDER") == "gemini"


def _is_gemini_tts() -> bool:
    return os.environ.get("STACKCHAN_TTS_PROVIDER") == "gemini"


def _is_glm_asr() -> bool:
    return os.environ.get("STACKCHAN_ASR_PROVIDER", "glm") == "glm"


def _is_glm_tts() -> bool:
    return os.environ.get("STACKCHAN_TTS_PROVIDER", "glm") == "glm"


def _gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""


def _gemini_generate(model: str, api_key: str, body: dict, timeout: int = 60) -> dict:
    endpoint = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    url = f"{endpoint}/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "stackchan-runtime-check/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:1000]}") from exc


def _gemini_response_text(response: dict) -> str:
    texts: list[str] = []
    for candidate in response.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
    return " ".join(texts).strip()


def _gemini_response_audio_bytes(response: dict) -> int:
    total = 0
    for candidate in response.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline_data = part.get("inline_data") or part.get("inlineData")
            if isinstance(inline_data, dict) and inline_data.get("data"):
                total += len(base64.b64decode(inline_data["data"]))
    return total


def _external_asr_gemini() -> bool:
    api_key = _gemini_api_key()
    if not api_key:
        _fail("external asr", "GEMINI_API_KEY or OPENAI_API_KEY is required")
        return False
    model = os.environ.get("STACKCHAN_ASR_MODEL", "gemini-2.5-flash")
    with tempfile.TemporaryDirectory(prefix="stackchan-check-") as tmp:
        wav_path = Path(tmp) / "tone.wav"
        _make_test_wav(wav_path)
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "请把这段音频转写成文字；如果只是测试音，请回复：测试音。"},
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": base64.b64encode(wav_path.read_bytes()).decode("ascii"),
                            }
                        },
                    ],
                }
            ]
        }
        try:
            text = _gemini_response_text(_gemini_generate(model, api_key, body))
            _ok("external asr", f"{model} -> {text[:40]!r}")
            return True
        except Exception as exc:
            _fail("external asr", f"{model}: {exc}")
            return False


def _external_asr() -> bool:
    if _is_gemini_asr():
        return _external_asr_gemini()
    if _is_glm_asr():
        from stackchan_asr_glm import _transcribe

        with tempfile.TemporaryDirectory(prefix="stackchan-check-") as tmp:
            wav_path = Path(tmp) / "tone.wav"
            _make_test_wav(wav_path)
            try:
                text = _transcribe(wav_path)
                _ok("external asr", f"{os.environ.get('STACKCHAN_ASR_MODEL', 'glm-asr-2512')} -> {text[:40]!r}")
                return True
            except Exception as exc:
                _fail("external asr", str(exc))
                return False

    try:
        from openai import OpenAI
    except Exception as exc:
        _fail("external asr", f"openai import failed: {exc}")
        return False

    model = os.environ.get("STACKCHAN_ASR_MODEL", "whisper-1")
    api_key = os.environ.get("STACKCHAN_ASR_API_KEY") or os.environ["OPENAI_API_KEY"]
    base_url = os.environ.get("STACKCHAN_ASR_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or None
    client = OpenAI(api_key=api_key, base_url=base_url)
    with tempfile.TemporaryDirectory(prefix="stackchan-check-") as tmp:
        wav_path = Path(tmp) / "tone.wav"
        _make_test_wav(wav_path)
        try:
            with wav_path.open("rb") as audio:
                result = client.audio.transcriptions.create(model=model, file=audio, language="zh")
            _ok("external asr", f"{model} -> {(getattr(result, 'text', '') or '')[:40]!r}")
            return True
        except Exception as exc:
            _fail("external asr", f"{model}: {exc}")
    return False


def _external_tts_gemini() -> bool:
    api_key = _gemini_api_key()
    if not api_key:
        _fail("external tts", "GEMINI_API_KEY or OPENAI_API_KEY is required")
        return False
    model = os.environ.get("STACKCHAN_TTS_MODEL", "gemini-2.5-flash-preview-tts")
    voice = os.environ.get("STACKCHAN_TTS_VOICE", "Kore")
    body = {
        "contents": [{"role": "user", "parts": [{"text": "StackChan Nanobot runtime check."}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice,
                    }
                }
            },
        },
    }
    try:
        byte_count = _gemini_response_audio_bytes(_gemini_generate(model, api_key, body))
        if byte_count <= 0:
            raise RuntimeError("response did not contain inline audio data")
        _ok("external tts", f"{model}/{voice} bytes={byte_count}")
        return True
    except Exception as exc:
        _fail("external tts", f"{model}/{voice}: {exc}")
        return False


def _external_tts() -> bool:
    if _is_gemini_tts():
        return _external_tts_gemini()
    if _is_glm_tts():
        from stackchan_tts_glm import _synthesize

        try:
            data = _synthesize("StackChan Nanobot runtime check.")
            if not data:
                raise RuntimeError("empty audio response")
            _ok("external tts", f"{os.environ.get('STACKCHAN_TTS_MODEL', 'glm-tts')} bytes={len(data)}")
            return True
        except Exception as exc:
            _fail("external tts", str(exc))
    return False


def _external_vision() -> bool:
    if os.environ.get("STACKCHAN_VISION_PROVIDER", "glm") != "glm":
        _warn("external vision", "skipped because STACKCHAN_VISION_PROVIDER is not glm")
        return True
    with tempfile.TemporaryDirectory(prefix="stackchan-check-") as tmp:
        image_path = Path(tmp) / "vision.jpg"
        generated = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=white:s=32x32:d=0.1",
                "-frames:v",
                "1",
                str(image_path),
            ],
            capture_output=True,
            text=True,
        )
        if generated.returncode != 0:
            _fail("external vision", generated.stderr[:500])
            return False
        env = os.environ.copy()
        env.update(
            {
                "STACKCHAN_VISION_IMAGE": str(image_path),
                "STACKCHAN_VISION_QUESTION": "这是一张测试图片，请用一个词描述主要颜色。",
            }
        )
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/stackchan_vision_glm.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if completed.returncode != 0:
            _fail("external vision", completed.stderr[:1000])
            return False
        _ok(
            "external vision",
            f"{os.environ.get('STACKCHAN_VISION_MODEL', 'glm-4.6v-flash')} -> {completed.stdout.strip()[:40]!r}",
        )
        return True

    try:
        from openai import OpenAI
    except Exception as exc:
        _fail("external tts", f"openai import failed: {exc}")
        return False

    model = os.environ.get("STACKCHAN_TTS_MODEL", "tts-1")
    voice = os.environ.get("STACKCHAN_TTS_VOICE", "alloy")
    api_key = os.environ.get("STACKCHAN_TTS_API_KEY") or os.environ["OPENAI_API_KEY"]
    base_url = os.environ.get("STACKCHAN_TTS_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or None
    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        result = client.audio.speech.create(
            model=model,
            voice=voice,
            input="StackChan Nanobot runtime check.",
            response_format="mp3",
        )
        data = result.read() if hasattr(result, "read") else b""
        _ok("external tts", f"{model}/{voice} bytes={len(data)}")
        return True
    except Exception as exc:
        _fail("external tts", f"{model}/{voice}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Check StackChan Nanobot runtime prerequisites")
    parser.add_argument("--config", default=str(ROOT / "nanobot_config" / "config.json"))
    parser.add_argument("--external", action="store_true", help="call configured external model endpoints")
    parser.add_argument(
        "--chat-model-candidates",
        default=os.environ.get("STACKCHAN_CHAT_MODEL_CANDIDATES", ""),
        help="comma-separated chat model candidates to probe when --external is used",
    )
    args = parser.parse_args()

    _configure_chat_provider_env()
    _configure_provider_proxy_bypass()

    config_path = Path(args.config)
    ok = _check_files(config_path)
    ok = _check_commands() and ok
    ok = _check_python_imports() and ok

    try:
        config = _load_config(config_path)
    except Exception as exc:
        _fail("config", str(exc))
        return 1
    ok = _check_env(config) and ok

    if args.external:
        if not os.environ.get("OPENAI_API_KEY"):
            _fail("external chat", "OPENAI_API_KEY is required")
            ok = False
        else:
            ok = _external_chat(config, _chat_model_candidates(config, args.chat_model_candidates)) and ok

        if not (os.environ.get("STACKCHAN_ASR_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            _fail("external asr", "STACKCHAN_ASR_API_KEY or OPENAI_API_KEY is required")
            ok = False
        else:
            ok = _external_asr() and ok

        if not (os.environ.get("STACKCHAN_TTS_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            _fail("external tts", "STACKCHAN_TTS_API_KEY or OPENAI_API_KEY is required")
            ok = False
        else:
            ok = _external_tts() and ok

        if not (
            os.environ.get("STACKCHAN_VISION_API_KEY")
            or os.environ.get("ZHIPU_API_KEY")
            or os.environ.get("GLM_API_KEY")
        ):
            _fail("external vision", "STACKCHAN_VISION_API_KEY, ZHIPU_API_KEY, or GLM_API_KEY is required")
            ok = False
        else:
            ok = _external_vision() and ok
    else:
        _warn("external", "skipped; pass --external to test chat/ASR/TTS model endpoints")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
