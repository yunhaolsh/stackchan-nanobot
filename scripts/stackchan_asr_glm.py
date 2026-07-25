#!/usr/bin/env python3
"""Transcribe StackChan Opus frames with the domestic Zhipu GLM ASR API."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from stackchan_audio import decode_stackchan_frames_to_wav
from stackchan_glm_http import request_bytes


def _api_key() -> str:
    value = (
        os.environ.get("STACKCHAN_ASR_API_KEY")
        or os.environ.get("ZHIPU_API_KEY")
        or os.environ.get("GLM_API_KEY")
    )
    if not value:
        raise RuntimeError("STACKCHAN_ASR_API_KEY, ZHIPU_API_KEY, or GLM_API_KEY is not set")
    return value


def _multipart_file(fields: dict[str, str], field_name: str, path: Path) -> tuple[bytes, str]:
    boundary = f"----stackchan-{secrets.token_hex(16)}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="{field_name}"; filename="speech.wav"\r\n'.encode()
    )
    body.extend(b"Content-Type: audio/wav\r\n\r\n")
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    return bytes(body), boundary


def _extract_text(payload: dict) -> str:
    direct = payload.get("text")
    if isinstance(direct, str):
        return direct.strip()
    for choice in payload.get("choices", []):
        message = choice.get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        return data["text"].strip()
    raise RuntimeError("GLM ASR response did not contain transcription text")


def _transcribe(wav_path: Path) -> str:
    base_url = os.environ.get("STACKCHAN_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    model = os.environ.get("STACKCHAN_ASR_MODEL", "glm-asr-2512")
    fields = {"model": model, "stream": "false"}
    prompt = os.environ.get("STACKCHAN_ASR_PROMPT", "").strip()
    if prompt:
        fields["prompt"] = prompt
    hotwords = os.environ.get("STACKCHAN_ASR_HOTWORDS", "").strip()
    if hotwords:
        parsed_hotwords = json.loads(hotwords)
        if not isinstance(parsed_hotwords, list) or not all(
            isinstance(item, str) for item in parsed_hotwords
        ):
            raise ValueError("STACKCHAN_ASR_HOTWORDS must be a JSON string array")
        fields["hotwords"] = json.dumps(parsed_hotwords, ensure_ascii=False)
    body, boundary = _multipart_file(fields, "file", wav_path)
    request = urllib.request.Request(
        f"{base_url}/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "stackchan-glm-asr/1",
        },
        method="POST",
    )
    response = request_bytes(
        request,
        timeout=int(os.environ.get("STACKCHAN_ASR_TIMEOUT", "60")),
        label="GLM ASR",
    )
    payload = json.loads(response.data.decode("utf-8"))
    transcript = _extract_text(payload)
    provider_result = {
        "model": model,
        "text": transcript,
        "request_id": (
            response.headers.get("x-request-id")
            or response.headers.get("x-b3-traceid")
            or payload.get("id")
            or ""
        ),
        "response_keys": sorted(payload.keys()),
    }
    print(
        f"provider_result={json.dumps(provider_result, ensure_ascii=False, separators=(',', ':'))}",
        file=sys.stderr,
    )
    return transcript


def main() -> int:
    frames_path = os.environ.get("STACKCHAN_AUDIO_FRAMES")
    if not frames_path:
        print("STACKCHAN_AUDIO_FRAMES is not set", file=sys.stderr)
        return 2
    sample_rate = int(os.environ.get("STACKCHAN_AUDIO_SAMPLE_RATE", "16000"))
    frame_duration_ms = int(os.environ.get("STACKCHAN_AUDIO_FRAME_DURATION_MS", "60"))
    with tempfile.TemporaryDirectory(prefix="stackchan-asr-glm-") as directory:
        wav_path = Path(directory) / "speech.wav"
        if not decode_stackchan_frames_to_wav(
            Path(frames_path),
            wav_path,
            sample_rate=sample_rate,
            frame_duration_ms=frame_duration_ms,
        ):
            return 0
        debug_dir = os.environ.get("STACKCHAN_ASR_DEBUG_DIR", "").strip()
        if debug_dir:
            destination_dir = Path(debug_dir)
            destination_dir.mkdir(parents=True, exist_ok=True)
            debug_id = os.environ.get("STACKCHAN_ASR_DEBUG_ID", "turn")
            safe_id = "".join(character for character in debug_id if character.isalnum() or character in "-_")
            debug_path = destination_dir / f"{safe_id or 'turn'}.wav"
            shutil.copyfile(wav_path, debug_path)
            print(f"debug_wav={debug_path}", file=sys.stderr)
        transcript = _transcribe(wav_path)
    if transcript:
        print(transcript)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"stackchan GLM ASR failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
