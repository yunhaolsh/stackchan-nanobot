#!/usr/bin/env python3
"""Exercise local Chat, Tool Call, TTS, and ASR endpoints end to end."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass


@dataclass
class HttpResult:
    body: bytes
    elapsed_ms: int
    headers: dict[str, str]


def request(
    url: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    token: str = "",
    timeout: float = 120,
) -> HttpResult:
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as response:
            return HttpResult(
                body=response.read(),
                elapsed_ms=round((time.monotonic() - started) * 1000),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc


def json_request(url: str, payload: dict, *, token: str = "", timeout: float = 120) -> tuple[dict, int]:
    result = request(
        url,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        token=token,
        timeout=timeout,
    )
    return json.loads(result.body), result.elapsed_ms


def multipart_audio(wav_data: bytes, model: str) -> tuple[bytes, str]:
    boundary = f"stackchan-{uuid.uuid4().hex}"
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nzh\r\n".encode(),
        (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
            "Content-Type: audio/wav\r\n\r\n"
        ).encode(),
        wav_data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def assert_tool_call(payload: dict) -> tuple[str, dict]:
    message = payload["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    if not calls:
        raise RuntimeError(f"local LLM returned no structured tool call: {message}")
    function = calls[0].get("function") or {}
    arguments = function.get("arguments") or "{}"
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    return str(function.get("name", "")), arguments


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-base", default="http://127.0.0.1:18080/v1")
    parser.add_argument("--speech-base", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--chat-model", default="Qwen3-4B")
    parser.add_argument("--asr-model", default="SenseVoiceSmall")
    parser.add_argument("--tts-model", default="vits-melo-tts-zh_en")
    args = parser.parse_args()

    chat_token = os.environ.get("STACKCHAN_LOCAL_CHAT_API_KEY", "local-no-secret")
    speech_token = os.environ.get("STACKCHAN_LOCAL_SPEECH_TOKEN", "")

    chat_health = request(args.chat_base.removesuffix("/v1") + "/health", timeout=10)
    speech_health = request(args.speech_base.removesuffix("/v1") + "/health", timeout=10)
    print(f"[check] chat health status=ok elapsed_ms={chat_health.elapsed_ms}")
    print(f"[check] speech health status=ok elapsed_ms={speech_health.elapsed_ms}")

    common = {
        "model": args.chat_model,
        "temperature": 0.1,
        "max_tokens": 128,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    chat, chat_ms = json_request(
        f"{args.chat_base}/chat/completions",
        {
            **common,
            "messages": [
                {"role": "system", "content": "请用一句简短中文回答。"},
                {"role": "user", "content": "请回复：本地模型已连接。"},
            ],
        },
        token=chat_token,
    )
    reply = str(chat["choices"][0]["message"].get("content") or "").strip()
    if not reply:
        raise RuntimeError(f"local LLM returned empty content: {chat}")
    print(f"[check] chat reply={reply!r} elapsed_ms={chat_ms}")

    tool_reply, tool_ms = json_request(
        f"{args.chat_base}/chat/completions",
        {
            **common,
            "messages": [
                {
                    "role": "system",
                    "content": "用户要求倒计时时必须调用 timer_start，不要只用文字回答。",
                },
                {"role": "user", "content": "设置一个20秒倒计时。"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "timer_start",
                        "description": "Start a named countdown timer on the device.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "duration_seconds": {"type": "integer", "minimum": 1},
                            },
                            "required": ["duration_seconds"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        },
        token=chat_token,
    )
    tool_name, tool_args = assert_tool_call(tool_reply)
    if tool_name != "timer_start" or tool_args.get("duration_seconds") != 20:
        raise RuntimeError(f"unexpected tool call: {tool_name}({tool_args})")
    print(f"[check] tool_call={tool_name} args={tool_args} elapsed_ms={tool_ms}")

    tts = request(
        f"{args.speech_base}/audio/speech",
        body=json.dumps(
            {
                "model": args.tts_model,
                "voice": "default",
                "input": "你好小智，本地语音服务已连接。",
                "response_format": "wav",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        content_type="application/json",
        token=speech_token,
    )
    if not tts.body.startswith(b"RIFF"):
        raise RuntimeError("local TTS did not return a WAV file")
    print(f"[check] tts bytes={len(tts.body)} elapsed_ms={tts.elapsed_ms}")

    asr_body, asr_type = multipart_audio(tts.body, args.asr_model)
    asr = request(
        f"{args.speech_base}/audio/transcriptions",
        body=asr_body,
        content_type=asr_type,
        token=speech_token,
    )
    transcript = str(json.loads(asr.body).get("text") or "").strip()
    if not transcript:
        raise RuntimeError("local ASR returned an empty transcript for local TTS audio")
    print(f"[check] asr transcript={transcript!r} elapsed_ms={asr.elapsed_ms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
