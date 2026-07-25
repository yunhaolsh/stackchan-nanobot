#!/usr/bin/env python3
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def _read_ogg_packets(path: Path) -> list[bytes]:
    data = path.read_bytes()
    packets: list[bytes] = []
    partial = bytearray()
    offset = 0
    while offset < len(data):
        if data[offset:offset + 4] != b"OggS":
            raise ValueError(f"missing OggS capture pattern at offset {offset}")
        if offset + 27 > len(data):
            raise ValueError("truncated Ogg page header")
        page_segments = data[offset + 26]
        segment_table_start = offset + 27
        segment_table_end = segment_table_start + page_segments
        if segment_table_end > len(data):
            raise ValueError("truncated Ogg segment table")
        lacing = data[segment_table_start:segment_table_end]
        body_start = segment_table_end
        body_end = body_start + sum(lacing)
        if body_end > len(data):
            raise ValueError("truncated Ogg page body")
        pos = body_start
        for size in lacing:
            partial.extend(data[pos:pos + size])
            pos += size
            if size < 255:
                packets.append(bytes(partial))
                partial.clear()
        offset = body_end
    if partial:
        packets.append(bytes(partial))
    return packets


def _gemini_generate(model: str, api_key: str, body: dict) -> dict:
    endpoint = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    url = f"{endpoint}/models/{model}:generateContent?key={api_key}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "stackchan-gemini-tts/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=int(os.environ.get("STACKCHAN_TTS_TIMEOUT", "60"))) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini TTS HTTP {exc.code}: {detail[:1000]}") from exc


def _extract_audio(response: dict) -> tuple[bytes, str]:
    for candidate in response.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            inline_data = part.get("inline_data") or part.get("inlineData")
            if isinstance(inline_data, dict) and inline_data.get("data"):
                mime_type = inline_data.get("mime_type") or inline_data.get("mimeType") or "audio/L16;codec=pcm;rate=24000"
                return base64.b64decode(inline_data["data"]), mime_type
    raise RuntimeError("Gemini TTS response did not contain inline audio data")


def _pcm_rate_from_mime(mime_type: str) -> int:
    match = re.search(r"rate=(\d+)", mime_type)
    return int(match.group(1)) if match else 24000


def _synthesize(input_path: Path, out_path: Path):
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY or OPENAI_API_KEY is not set")

    text = input_path.read_text(encoding="utf-8").strip()
    if not text:
        return

    model = os.environ.get("STACKCHAN_TTS_MODEL", "gemini-2.5-flash-preview-tts")
    voice = os.environ.get("STACKCHAN_TTS_VOICE", "Kore")
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": text}],
            }
        ],
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
    audio, mime_type = _extract_audio(_gemini_generate(model, api_key, body))
    if "wav" in mime_type.lower():
        out_path.write_bytes(audio)
        return
    rate = _pcm_rate_from_mime(mime_type)
    raw_path = out_path.with_suffix(".s16le")
    raw_path.write_bytes(audio)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(rate),
            "-ac",
            "1",
            "-i",
            str(raw_path),
            str(out_path),
        ],
        check=True,
    )


def main() -> int:
    input_path = os.environ.get("STACKCHAN_TTS_INPUT")
    if not input_path:
        print("STACKCHAN_TTS_INPUT is not set", file=sys.stderr)
        return 2

    sample_rate = int(os.environ.get("STACKCHAN_TTS_SAMPLE_RATE", "16000"))
    frame_duration_ms = int(os.environ.get("STACKCHAN_TTS_FRAME_DURATION_MS", "60"))

    with tempfile.TemporaryDirectory(prefix="stackchan-tts-gemini-") as tmp_dir:
        tmp = Path(tmp_dir)
        source_path = tmp / "speech.wav"
        ogg_path = tmp / "speech.ogg"
        _synthesize(Path(input_path), source_path)
        if not source_path.exists() or source_path.stat().st_size == 0:
            return 0
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_path),
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-c:a",
                "libopus",
                "-application",
                "voip",
                "-frame_duration",
                str(frame_duration_ms),
                str(ogg_path),
            ],
            check=True,
        )
        packets = _read_ogg_packets(ogg_path)

    audio_packets = [
        packet for packet in packets
        if not packet.startswith(b"OpusHead") and not packet.startswith(b"OpusTags")
    ]
    out = sys.stdout.buffer
    for packet in audio_packets:
        if len(packet) > 65535:
            raise ValueError("opus packet too large for StackChan v3 frame")
        out.write(struct.pack("!H", len(packet)))
        out.write(packet)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"stackchan Gemini TTS failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
