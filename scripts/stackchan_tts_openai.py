#!/usr/bin/env python3
import os
import struct
import subprocess
import sys
import tempfile
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


def _synthesize(input_path: Path, out_path: Path):
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError(f"openai package is not importable: {exc}") from exc

    api_key = os.environ.get("STACKCHAN_TTS_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("STACKCHAN_TTS_API_KEY or OPENAI_API_KEY is not set")

    kwargs = {"api_key": api_key}
    base_url = os.environ.get("STACKCHAN_TTS_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    text = input_path.read_text(encoding="utf-8").strip()
    if not text:
        return

    model = os.environ.get("STACKCHAN_TTS_MODEL", "tts-1")
    voice = os.environ.get("STACKCHAN_TTS_VOICE", "alloy")
    response_format = os.environ.get("STACKCHAN_TTS_RESPONSE_FORMAT", "mp3")
    response = client.audio.speech.create(
        model=model,
        voice=voice,
        input=text,
        response_format=response_format,
    )
    response.write_to_file(out_path)


def main() -> int:
    input_path = os.environ.get("STACKCHAN_TTS_INPUT")
    if not input_path:
        print("STACKCHAN_TTS_INPUT is not set", file=sys.stderr)
        return 2

    sample_rate = int(os.environ.get("STACKCHAN_TTS_SAMPLE_RATE", "16000"))
    frame_duration_ms = int(os.environ.get("STACKCHAN_TTS_FRAME_DURATION_MS", "60"))

    with tempfile.TemporaryDirectory(prefix="stackchan-tts-") as tmp_dir:
        tmp = Path(tmp_dir)
        response_format = os.environ.get("STACKCHAN_TTS_RESPONSE_FORMAT", "mp3")
        source_path = tmp / f"speech.{response_format}"
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
        print(f"stackchan TTS failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
