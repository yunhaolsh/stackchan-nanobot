"""Server-side utterance endpointing for StackChan auto-listen audio."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import opuslib_next
import webrtcvad


@dataclass(frozen=True)
class EndpointDecision:
    complete: bool
    reason: str = ""
    duration_ms: int = 0
    speech_ms: int = 0
    silence_ms: int = 0
    speech_start_packet: int = -1
    speech_end_packet: int = -1
    error: str = ""


class AudioEndpoint:
    def __init__(
        self,
        *,
        sample_rate: int,
        packet_duration_ms: int,
        aggressiveness: int = 2,
        vad_frame_ms: int = 20,
        min_speech_ms: int = 240,
        end_silence_ms: int = 900,
        no_speech_timeout_ms: int = 8000,
        max_duration_ms: int = 20000,
        decoder_factory: Callable[[int, int], Any] = opuslib_next.Decoder,
        vad_factory: Callable[[int], Any] = webrtcvad.Vad,
    ):
        if sample_rate not in {8000, 16000, 32000, 48000}:
            raise ValueError(f"unsupported WebRTC VAD sample rate: {sample_rate}")
        if packet_duration_ms <= 0 or vad_frame_ms not in {10, 20, 30}:
            raise ValueError("invalid audio frame duration")
        self.sample_rate = sample_rate
        self.packet_duration_ms = packet_duration_ms
        self.vad_frame_ms = vad_frame_ms
        self.min_speech_ms = min_speech_ms
        self.end_silence_ms = end_silence_ms
        self.no_speech_timeout_ms = no_speech_timeout_ms
        self.max_duration_ms = max_duration_ms
        self.decoder = decoder_factory(sample_rate, 1)
        self.vad = vad_factory(aggressiveness)
        self.duration_ms = 0
        self.speech_ms = 0
        self.speech_run_ms = 0
        self.silence_ms = 0
        self.speech_started = False
        self.packet_count = 0
        self.candidate_start_packet = -1
        self.speech_start_packet = -1
        self.speech_end_packet = -1
        self.decode_errors = 0

    def add_packet(self, opus_packet: bytes) -> EndpointDecision:
        packet_index = self.packet_count
        self.packet_count += 1
        self.duration_ms += self.packet_duration_ms
        error = ""
        voiced_ms = 0
        try:
            packet_samples = self.sample_rate * self.packet_duration_ms // 1000
            pcm = self.decoder.decode(opus_packet, packet_samples, decode_fec=False)
            chunk_bytes = self.sample_rate * self.vad_frame_ms // 1000 * 2
            for offset in range(0, len(pcm) - chunk_bytes + 1, chunk_bytes):
                if self.vad.is_speech(pcm[offset : offset + chunk_bytes], self.sample_rate):
                    voiced_ms += self.vad_frame_ms
        except Exception as exc:
            self.decode_errors += 1
            error = str(exc)

        if voiced_ms:
            if self.speech_run_ms == 0:
                self.candidate_start_packet = packet_index
            self.speech_ms += voiced_ms
            self.speech_run_ms += voiced_ms
            self.silence_ms = 0
            if self.speech_run_ms >= self.min_speech_ms:
                self.speech_started = True
                if self.speech_start_packet < 0:
                    self.speech_start_packet = self.candidate_start_packet
            if self.speech_started:
                self.speech_end_packet = packet_index + 1
        elif self.speech_started:
            self.silence_ms += self.packet_duration_ms
        else:
            self.speech_run_ms = 0
            self.candidate_start_packet = -1

        reason = ""
        if self.speech_started and self.silence_ms >= self.end_silence_ms:
            reason = "end_silence"
        elif not self.speech_started and self.duration_ms >= self.no_speech_timeout_ms:
            reason = "no_speech_timeout"
        elif self.duration_ms >= self.max_duration_ms:
            reason = "max_duration"

        return EndpointDecision(
            complete=bool(reason),
            reason=reason,
            duration_ms=self.duration_ms,
            speech_ms=self.speech_ms,
            silence_ms=self.silence_ms,
            speech_start_packet=self.speech_start_packet,
            speech_end_packet=self.speech_end_packet,
            error=error,
        )


def trim_to_speech(
    frames: list[bytes],
    decision: EndpointDecision,
    *,
    packet_duration_ms: int,
    pre_roll_ms: int = 240,
    post_roll_ms: int = 300,
) -> list[bytes]:
    if decision.speech_start_packet < 0 or decision.speech_end_packet < 0:
        return frames
    pre_roll_packets = max(0, pre_roll_ms) // packet_duration_ms
    post_roll_packets = max(0, post_roll_ms) // packet_duration_ms
    start = max(0, decision.speech_start_packet - pre_roll_packets)
    end = min(len(frames), decision.speech_end_packet + post_roll_packets)
    return frames[start:end]
