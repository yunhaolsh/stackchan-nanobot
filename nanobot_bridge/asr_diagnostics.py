"""Audio evidence and inferred failure reasons for StackChan ASR turns."""

from __future__ import annotations

import math
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioSignalMetrics:
    duration_ms: int
    sample_rate: int
    rms_dbfs: float
    peak_dbfs: float
    clipping_ratio: float
    low_energy_ratio: float


def _dbfs(value: float) -> float:
    if value <= 0:
        return -96.0
    return max(-96.0, 20.0 * math.log10(value / 32768.0))


def analyze_wav(path: str | Path, *, window_ms: int = 20) -> AudioSignalMetrics:
    """Measure a mono/stereo 16-bit PCM WAV without external dependencies."""
    with wave.open(str(path), "rb") as source:
        sample_rate = source.getframerate()
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        frame_count = source.getnframes()
        pcm = source.readframes(frame_count)

    if sample_width != 2:
        raise ValueError(f"ASR diagnostic WAV must be 16-bit PCM, got {sample_width * 8}-bit")
    if channels <= 0 or sample_rate <= 0:
        raise ValueError("ASR diagnostic WAV has invalid channel or sample-rate metadata")

    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return AudioSignalMetrics(0, sample_rate, -96.0, -96.0, 0.0, 1.0)

    absolute_peak = max(abs(sample) for sample in samples)
    squared_sum = sum(sample * sample for sample in samples)
    rms = math.sqrt(squared_sum / len(samples))
    clipped = sum(abs(sample) >= 32700 for sample in samples)

    window_samples = max(1, sample_rate * channels * window_ms // 1000)
    low_energy_windows = 0
    window_count = 0
    for offset in range(0, len(samples), window_samples):
        window = samples[offset : offset + window_samples]
        if not window:
            continue
        window_rms = math.sqrt(sum(sample * sample for sample in window) / len(window))
        low_energy_windows += _dbfs(window_rms) < -42.0
        window_count += 1

    return AudioSignalMetrics(
        duration_ms=round(frame_count * 1000 / sample_rate),
        sample_rate=sample_rate,
        rms_dbfs=_dbfs(rms),
        peak_dbfs=_dbfs(absolute_peak),
        clipping_ratio=clipped / len(samples),
        low_energy_ratio=low_energy_windows / max(window_count, 1),
    )


def infer_no_transcript_reasons(
    *,
    endpoint_reason: str,
    vad_duration_ms: int,
    vad_speech_ms: int,
    speech_start_packet: int,
    trimmed_audio_ms: int,
    metrics: AudioSignalMetrics | None,
) -> list[str]:
    """Return evidence-based hypotheses, ordered from strongest to weakest."""
    reasons: list[str] = []
    if endpoint_reason == "no_speech_timeout":
        reasons.append("vad_no_speech_detected")
    if 0 < vad_speech_ms < 600:
        reasons.append("effective_speech_too_short")
    if 0 < trimmed_audio_ms < 1200:
        reasons.append("utterance_too_short_or_clipped")
    if speech_start_packet == 0:
        reasons.append("possible_leading_edge_clipped")
    if metrics is not None:
        if metrics.rms_dbfs < -35.0:
            reasons.append("input_level_too_low")
        if metrics.low_energy_ratio >= 0.75:
            reasons.append("audio_mostly_silence")
        if metrics.clipping_ratio >= 0.01:
            reasons.append("input_clipping")
    if endpoint_reason == "max_duration":
        reasons.append("vad_did_not_find_end_silence")
    if not reasons:
        reasons.append("provider_returned_no_transcript_without_reason")
    return reasons
