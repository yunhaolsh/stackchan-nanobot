#!/usr/bin/env python3
"""Synthesize StackChan reply audio with the domestic Zhipu GLM TTS API."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import opuslib_next

from stackchan_audio import encode_audio_to_length_prefixed_opus
from stackchan_glm_http import request_bytes


def _api_key() -> str:
    value = (
        os.environ.get("STACKCHAN_TTS_API_KEY")
        or os.environ.get("ZHIPU_API_KEY")
        or os.environ.get("GLM_API_KEY")
    )
    if not value:
        raise RuntimeError("STACKCHAN_TTS_API_KEY, ZHIPU_API_KEY, or GLM_API_KEY is not set")
    return value


def _decode_json_audio(payload: dict) -> bytes:
    for key in ("audio", "data", "audio_data"):
        value = payload.get(key)
        if isinstance(value, str):
            return base64.b64decode(value)
        if isinstance(value, dict):
            encoded = value.get("data") or value.get("audio")
            if isinstance(encoded, str):
                return base64.b64decode(encoded)
    raise RuntimeError("GLM TTS JSON response did not contain audio")


def _synthesize(text: str) -> bytes:
    base_url = os.environ.get("STACKCHAN_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    body = json.dumps(
        {
            "model": os.environ.get("STACKCHAN_TTS_MODEL", "glm-tts"),
            "input": text,
            "voice": os.environ.get("STACKCHAN_TTS_VOICE", "tongtong"),
            "response_format": "wav",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/audio/speech",
        data=body,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "User-Agent": "stackchan-glm-tts/1",
        },
        method="POST",
    )
    response = request_bytes(
        request,
        timeout=int(os.environ.get("STACKCHAN_TTS_TIMEOUT", "90")),
        label="GLM TTS",
    )
    data = response.data
    content_type = response.headers.get_content_type()
    if content_type == "application/json" or data.lstrip().startswith(b"{"):
        return _decode_json_audio(json.loads(data.decode("utf-8")))
    return data


def _streaming_pcm(text: str) -> Iterator[bytes]:
    base_url = os.environ.get("STACKCHAN_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    body = json.dumps(
        {
            "model": os.environ.get("STACKCHAN_TTS_MODEL", "glm-tts"),
            "input": text,
            "voice": os.environ.get("STACKCHAN_TTS_VOICE", "tongtong"),
            "response_format": "pcm",
            "encode_format": "base64",
            "stream": True,
            "speed": float(os.environ.get("STACKCHAN_TTS_SPEED", "1.0")),
            "volume": float(os.environ.get("STACKCHAN_TTS_VOLUME", "1.0")),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/audio/speech",
        data=body,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "stackchan-glm-tts/1",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(
            request, timeout=int(os.environ.get("STACKCHAN_TTS_TIMEOUT", "90"))
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GLM TTS HTTP {exc.code}: {detail[:1000]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"GLM TTS network error: {exc}") from exc

    with response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            if event.get("error"):
                raise RuntimeError(f"GLM TTS stream error: {event['error']}")
            for choice in event.get("choices", []):
                if choice.get("finish_reason") == "stop":
                    continue
                delta = choice.get("delta") or {}
                encoded = delta.get("content")
                if not encoded:
                    continue
                source_rate = int(delta.get("return_sample_rate", 24000))
                expected_rate = int(os.environ.get("STACKCHAN_TTS_SOURCE_SAMPLE_RATE", "24000"))
                if source_rate != expected_rate:
                    raise RuntimeError(
                        f"GLM TTS returned {source_rate} Hz PCM, expected {expected_rate} Hz"
                    )
                yield base64.b64decode(encoded, validate=True)


def _encode_pcm_chunks(pcm_chunks: Iterator[bytes], source_rate: int, frame_duration_ms: int) -> Iterator[bytes]:
    frame_samples = source_rate * frame_duration_ms // 1000
    frame_bytes = frame_samples * 2
    encoder = opuslib_next.Encoder(source_rate, 1, opuslib_next.APPLICATION_VOIP)
    encoder.bitrate = int(os.environ.get("STACKCHAN_TTS_OPUS_BITRATE", "24000"))
    encoder.signal = opuslib_next.SIGNAL_VOICE
    pending = bytearray()
    for pcm in pcm_chunks:
        pending.extend(pcm)
        while len(pending) >= frame_bytes:
            frame = bytes(pending[:frame_bytes])
            del pending[:frame_bytes]
            yield encoder.encode(frame, frame_samples)
    if pending:
        pending.extend(bytes(frame_bytes - len(pending)))
        yield encoder.encode(bytes(pending), frame_samples)


def _stream_pcm_as_opus(text: str, sample_rate: int, frame_duration_ms: int) -> None:
    source_rate = int(os.environ.get("STACKCHAN_TTS_SOURCE_SAMPLE_RATE", "24000"))
    packet_count = 0
    for packet in _encode_pcm_chunks(_streaming_pcm(text), source_rate, frame_duration_ms):
        if len(packet) > 65535:
            raise ValueError("opus packet too large for StackChan v3 frame")
        sys.stdout.buffer.write(len(packet).to_bytes(2, "big"))
        sys.stdout.buffer.write(packet)
        sys.stdout.buffer.flush()
        packet_count += 1
    if packet_count == 0:
        raise RuntimeError("GLM TTS stream returned no PCM audio")


def main() -> int:
    input_path = os.environ.get("STACKCHAN_TTS_INPUT")
    if not input_path:
        print("STACKCHAN_TTS_INPUT is not set", file=sys.stderr)
        return 2
    text = Path(input_path).read_text(encoding="utf-8").strip()
    if not text:
        return 0
    sample_rate = int(os.environ.get("STACKCHAN_TTS_SAMPLE_RATE", "16000"))
    frame_duration_ms = int(os.environ.get("STACKCHAN_TTS_FRAME_DURATION_MS", "60"))
    if os.environ.get("STACKCHAN_TTS_STREAMING", "1") == "1":
        _stream_pcm_as_opus(text, sample_rate, frame_duration_ms)
        return 0
    with tempfile.TemporaryDirectory(prefix="stackchan-tts-glm-") as directory:
        wav_path = Path(directory) / "speech.wav"
        wav_path.write_bytes(_synthesize(text))
        output = encode_audio_to_length_prefixed_opus(
            wav_path,
            sample_rate=sample_rate,
            frame_duration_ms=frame_duration_ms,
        )
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"stackchan GLM TTS failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
