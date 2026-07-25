#!/usr/bin/env python3
"""Audio framing helpers shared by StackChan ASR/TTS adapters."""

from __future__ import annotations

import os
import struct
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO


def _crc_table() -> list[int]:
    table = []
    for value in range(256):
        remainder = value << 24
        for _ in range(8):
            if remainder & 0x80000000:
                remainder = ((remainder << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                remainder = (remainder << 1) & 0xFFFFFFFF
        table.append(remainder)
    return table


CRC_TABLE = _crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _ogg_page(packet: bytes, *, serial: int, sequence: int, granule: int, header_type: int) -> bytes:
    full, tail = divmod(len(packet), 255)
    lacing = bytes([255] * full + [tail])
    header = (
        b"OggS"
        + bytes([0, header_type])
        + struct.pack("<QIIi", granule, serial, sequence, 0)
        + bytes([len(lacing)])
        + lacing
    )
    page = header + packet
    return page[:22] + struct.pack("<I", _ogg_crc(page)) + page[26:]


def read_length_prefixed_frames(path: Path) -> list[bytes]:
    data = path.read_bytes()
    frames: list[bytes] = []
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("truncated frame length")
        size = struct.unpack("!H", data[offset : offset + 2])[0]
        offset += 2
        if offset + size > len(data):
            raise ValueError("truncated opus frame payload")
        frames.append(data[offset : offset + size])
        offset += size
    return frames


def write_ogg_opus(
    frames: list[bytes], out_path: Path, sample_rate: int, frame_duration_ms: int
) -> None:
    serial = int.from_bytes(os.urandom(4), "little")
    sequence = 0
    granule = 0
    granule_step = int(48000 * frame_duration_ms / 1000)
    opus_head = b"OpusHead" + bytes([1, 1]) + struct.pack("<HIhB", 312, sample_rate, 0, 0)
    vendor = b"stackchan-nanobot"
    opus_tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)

    with out_path.open("wb") as output:
        output.write(_ogg_page(opus_head, serial=serial, sequence=sequence, granule=0, header_type=0x02))
        sequence += 1
        output.write(_ogg_page(opus_tags, serial=serial, sequence=sequence, granule=0, header_type=0))
        sequence += 1
        for index, frame in enumerate(frames):
            granule += granule_step
            output.write(
                _ogg_page(
                    frame,
                    serial=serial,
                    sequence=sequence,
                    granule=granule,
                    header_type=0x04 if index == len(frames) - 1 else 0,
                )
            )
            sequence += 1


def decode_stackchan_frames_to_wav(
    frames_path: Path,
    wav_path: Path,
    *,
    sample_rate: int,
    frame_duration_ms: int,
) -> bool:
    frames = read_length_prefixed_frames(frames_path)
    if not frames:
        return False
    ogg_path = wav_path.with_suffix(".ogg")
    write_ogg_opus(frames, ogg_path, sample_rate, frame_duration_ms)
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
    return True


def read_ogg_packets(path: Path) -> list[bytes]:
    data = path.read_bytes()
    packets: list[bytes] = []
    partial = bytearray()
    offset = 0
    while offset < len(data):
        if data[offset : offset + 4] != b"OggS":
            raise ValueError(f"missing OggS capture pattern at offset {offset}")
        if offset + 27 > len(data):
            raise ValueError("truncated Ogg page header")
        segment_count = data[offset + 26]
        table_start = offset + 27
        table_end = table_start + segment_count
        if table_end > len(data):
            raise ValueError("truncated Ogg segment table")
        lacing = data[table_start:table_end]
        body_start = table_end
        body_end = body_start + sum(lacing)
        if body_end > len(data):
            raise ValueError("truncated Ogg page body")
        position = body_start
        for size in lacing:
            partial.extend(data[position : position + size])
            position += size
            if size < 255:
                packets.append(bytes(partial))
                partial.clear()
        offset = body_end
    if partial:
        packets.append(bytes(partial))
    return packets


def iter_ogg_packets(stream: BinaryIO) -> Iterator[bytes]:
    """Yield complete packets from an Ogg byte stream as pages arrive."""

    def read_exact(size: int, *, allow_eof: bool = False) -> bytes:
        output = bytearray()
        while len(output) < size:
            chunk = stream.read(size - len(output))
            if not chunk:
                if allow_eof and not output:
                    return b""
                raise ValueError("truncated Ogg stream")
            output.extend(chunk)
        return bytes(output)

    partial = bytearray()
    while True:
        header = read_exact(27, allow_eof=True)
        if not header:
            break
        if header[:4] != b"OggS":
            raise ValueError("missing OggS capture pattern in stream")
        segment_count = header[26]
        lacing = read_exact(segment_count)
        body = read_exact(sum(lacing))
        offset = 0
        for size in lacing:
            partial.extend(body[offset : offset + size])
            offset += size
            if size < 255:
                yield bytes(partial)
                partial.clear()
    if partial:
        raise ValueError("truncated Ogg packet at end of stream")


def encode_audio_to_length_prefixed_opus(
    source_path: Path,
    *,
    sample_rate: int,
    frame_duration_ms: int,
) -> bytes:
    ogg_path = source_path.with_suffix(".opus.ogg")
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
    output = bytearray()
    for packet in read_ogg_packets(ogg_path):
        if packet.startswith((b"OpusHead", b"OpusTags")):
            continue
        if len(packet) > 65535:
            raise ValueError("opus packet too large for StackChan v3 frame")
        output.extend(struct.pack("!H", len(packet)))
        output.extend(packet)
    return bytes(output)
