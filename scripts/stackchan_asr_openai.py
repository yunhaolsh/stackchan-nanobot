#!/usr/bin/env python3
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


def _crc_table():
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            if r & 0x80000000:
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                r = (r << 1) & 0xFFFFFFFF
        table.append(r)
    return table


CRC_TABLE = _crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _packet_lacing(packet: bytes) -> bytes:
    full, tail = divmod(len(packet), 255)
    return bytes([255] * full + [tail])


def _ogg_page(packet: bytes, *, serial: int, sequence: int, granule: int, header_type: int) -> bytes:
    lacing = _packet_lacing(packet)
    header = (
        b"OggS"
        + bytes([0, header_type])
        + struct.pack("<QIIi", granule, serial, sequence, 0)
        + bytes([len(lacing)])
        + lacing
    )
    page = header + packet
    checksum = _ogg_crc(page)
    return page[:22] + struct.pack("<I", checksum) + page[26:]


def _read_length_prefixed_frames(path: Path) -> list[bytes]:
    data = path.read_bytes()
    frames = []
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("truncated frame length")
        size = struct.unpack("!H", data[offset:offset + 2])[0]
        offset += 2
        if offset + size > len(data):
            raise ValueError("truncated opus frame payload")
        frames.append(data[offset:offset + size])
        offset += size
    return frames


def _write_ogg_opus(frames: list[bytes], out_path: Path, sample_rate: int, frame_duration_ms: int):
    serial = int.from_bytes(os.urandom(4), "little")
    seq = 0
    granule = 0
    granule_step = int(48000 * frame_duration_ms / 1000)

    opus_head = (
        b"OpusHead"
        + bytes([1, 1])
        + struct.pack("<HIhB", 312, sample_rate, 0, 0)
    )
    opus_tags = b"OpusTags" + struct.pack("<I", len(b"stackchan-nanobot")) + b"stackchan-nanobot" + struct.pack("<I", 0)

    with out_path.open("wb") as f:
        f.write(_ogg_page(opus_head, serial=serial, sequence=seq, granule=0, header_type=0x02))
        seq += 1
        f.write(_ogg_page(opus_tags, serial=serial, sequence=seq, granule=0, header_type=0x00))
        seq += 1
        for index, frame in enumerate(frames):
            granule += granule_step
            header_type = 0x04 if index == len(frames) - 1 else 0x00
            f.write(_ogg_page(frame, serial=serial, sequence=seq, granule=granule, header_type=header_type))
            seq += 1


def _transcribe(wav_path: Path) -> str:
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError(f"openai package is not importable: {exc}") from exc

    api_key = os.environ.get("STACKCHAN_ASR_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("STACKCHAN_ASR_API_KEY or OPENAI_API_KEY is not set")

    kwargs = {"api_key": api_key}
    base_url = os.environ.get("STACKCHAN_ASR_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    model = os.environ.get("STACKCHAN_ASR_MODEL", "whisper-1")
    language = os.environ.get("STACKCHAN_ASR_LANGUAGE", "zh")
    with wav_path.open("rb") as audio:
        result = client.audio.transcriptions.create(
            model=model,
            file=audio,
            language=language,
        )
    return (getattr(result, "text", None) or "").strip()


def main() -> int:
    frames_path = os.environ.get("STACKCHAN_AUDIO_FRAMES")
    if not frames_path:
        print("STACKCHAN_AUDIO_FRAMES is not set", file=sys.stderr)
        return 2

    sample_rate = int(os.environ.get("STACKCHAN_AUDIO_SAMPLE_RATE", "16000"))
    frame_duration_ms = int(os.environ.get("STACKCHAN_AUDIO_FRAME_DURATION_MS", "60"))
    frames = _read_length_prefixed_frames(Path(frames_path))
    if not frames:
        return 0

    with tempfile.TemporaryDirectory(prefix="stackchan-asr-") as tmp_dir:
        ogg_path = Path(tmp_dir) / "speech.ogg"
        wav_path = Path(tmp_dir) / "speech.wav"
        _write_ogg_opus(frames, ogg_path, sample_rate, frame_duration_ms)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(ogg_path),
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                str(wav_path),
            ],
            check=True,
        )
        transcript = _transcribe(wav_path)
    if transcript:
        print(transcript)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"stackchan ASR failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
