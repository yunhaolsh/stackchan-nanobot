from __future__ import annotations

import sys
from pathlib import Path
import socket
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "nanobot_bridge"))

from audio_endpoint import AudioEndpoint, EndpointDecision, trim_to_speech  # noqa: E402
from asr_diagnostics import analyze_wav, infer_no_transcript_reasons  # noqa: E402
import server  # noqa: E402


class FakeDecoder:
    def __init__(self, sample_rate: int, _channels: int):
        self.sample_rate = sample_rate

    def decode(self, _packet: bytes, frame_size: int, decode_fec: bool = False) -> bytes:
        assert not decode_fec
        return b"\0\0" * frame_size


class FakeVad:
    results: list[bool] = []

    def __init__(self, _aggressiveness: int):
        self._results = iter(self.results)

    def is_speech(self, _pcm: bytes, _sample_rate: int) -> bool:
        return next(self._results, False)


def make_endpoint(vad_results: list[bool], **overrides) -> AudioEndpoint:
    FakeVad.results = vad_results
    options = {
        "sample_rate": 16000,
        "packet_duration_ms": 60,
        "min_speech_ms": 240,
        "end_silence_ms": 900,
        "no_speech_timeout_ms": 8000,
        "max_duration_ms": 20000,
        "decoder_factory": FakeDecoder,
        "vad_factory": FakeVad,
    }
    options.update(overrides)
    return AudioEndpoint(**options)


def test_ends_after_speech_then_silence():
    # Four 60 ms voiced packets establish speech, then 15 silent packets provide 900 ms silence.
    endpoint = make_endpoint([True] * 12 + [False] * 45)
    decisions = [endpoint.add_packet(b"opus") for _ in range(19)]

    assert not any(decision.complete for decision in decisions[:-1])
    assert decisions[-1].complete
    assert decisions[-1].reason == "end_silence"
    assert decisions[-1].speech_ms == 240
    assert decisions[-1].silence_ms == 900


def test_no_speech_timeout_is_bounded():
    endpoint = make_endpoint(
        [False] * 9,
        no_speech_timeout_ms=180,
        max_duration_ms=1000,
    )

    decisions = [endpoint.add_packet(b"opus") for _ in range(3)]

    assert decisions[-1].complete
    assert decisions[-1].reason == "no_speech_timeout"


def test_decode_failure_still_reaches_max_duration():
    class FailingDecoder(FakeDecoder):
        def decode(self, *_args, **_kwargs):
            raise RuntimeError("invalid opus")

    endpoint = make_endpoint(
        [],
        decoder_factory=FailingDecoder,
        no_speech_timeout_ms=1000,
        max_duration_ms=120,
    )

    first = endpoint.add_packet(b"bad")
    second = endpoint.add_packet(b"bad")

    assert not first.complete
    assert second.complete
    assert second.reason == "max_duration"
    assert second.error == "invalid opus"


def test_trim_to_speech_keeps_small_pre_and_post_roll():
    frames = [bytes([index]) for index in range(20)]
    decision = EndpointDecision(
        complete=True,
        speech_start_packet=5,
        speech_end_packet=12,
    )

    trimmed = trim_to_speech(
        frames,
        decision,
        packet_duration_ms=60,
        pre_roll_ms=120,
        post_roll_ms=180,
    )

    assert trimmed == frames[3:15]


def test_wake_audio_guard_discards_popup_echo(monkeypatch):
    device, peer = socket.socketpair()
    try:
        monkeypatch.setenv("STACKCHAN_REQUIRE_WAKE_WORD", "0")
        session = server.ClientSession(device, device_key="device-1")
        session.frame_duration = 60
        session.wake_detected_at = server.time.monotonic()
        monkeypatch.setenv("STACKCHAN_WAKE_AUDIO_GUARD_MS", "720")

        session.start_listening("auto")
        assert session.wake_guard_packets_applied == 12
        for _ in range(12):
            assert session.append_audio(b"popup") is None
        assert session.frames == []
        assert session.wake_guard_packets_remaining == 0

        session.append_audio(b"user")
        assert session.frames == [b"user"]
    finally:
        device.close()
        peer.close()


def test_audio_is_gated_until_local_wake_event(monkeypatch):
    device, peer = socket.socketpair()
    try:
        monkeypatch.setenv("STACKCHAN_REQUIRE_WAKE_WORD", "1")
        monkeypatch.setenv("STACKCHAN_WAKE_AUDIO_GUARD_MS", "0")
        session = server.ClientSession(device, device_key="device-1")

        session.start_listening("auto")
        session.append_audio(b"office-conversation")
        assert session.frames == []

        session.arm_wake()
        session.start_listening("auto")
        session.append_audio(b"user-command")
        assert session.frames == [b"user-command"]

        session.stop_listening()
        session.consume_wake()
        session.start_listening("auto")
        session.append_audio(b"more-office-conversation")
        assert session.frames == []
    finally:
        device.close()
        peer.close()


def test_wake_abort_arms_the_next_capture_window(monkeypatch):
    device, peer = socket.socketpair()
    try:
        monkeypatch.setenv("STACKCHAN_REQUIRE_WAKE_WORD", "1")
        monkeypatch.setenv("STACKCHAN_WAKE_AUDIO_GUARD_MS", "0")
        session = server.ClientSession(device, device_key="device-1")
        handler = object.__new__(server.BridgeHandler)

        handler._handle_ws_text(
            session,
            '{"type":"abort","reason":"wake_word_detected"}',
        )
        handler._handle_ws_text(
            session,
            '{"type":"listen","state":"start","mode":"auto"}',
        )
        session.append_audio(b"user-command")

        assert session.wake_armed is True
        assert session.frames == [b"user-command"]
    finally:
        device.close()
        peer.close()


def test_wake_abort_cancels_inflight_tts(monkeypatch):
    device, peer = socket.socketpair()
    try:
        monkeypatch.setenv("STACKCHAN_REQUIRE_WAKE_WORD", "1")
        session = server.ClientSession(device, device_key="device-1")
        handler = object.__new__(server.BridgeHandler)
        generation = session.begin_tts()

        handler._handle_ws_text(
            session,
            '{"type":"abort","reason":"wake_word_detected"}',
        )

        assert session.is_tts_current(generation) is False
        assert session.wake_armed is True
    finally:
        device.close()
        peer.close()


def test_stale_session_messages_are_ignored(monkeypatch):
    device, peer = socket.socketpair()
    try:
        monkeypatch.setenv("STACKCHAN_REQUIRE_WAKE_WORD", "1")
        session = server.ClientSession(device, device_key="device-1")
        handler = object.__new__(server.BridgeHandler)

        handler._handle_ws_text(
            session,
            '{"session_id":"old-session","type":"listen","state":"start","mode":"auto"}',
        )

        assert session.listening is False
        assert session.wake_armed is False
    finally:
        device.close()
        peer.close()


def _write_pcm_wav(path: Path, samples: list[int], sample_rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples))


def test_asr_audio_metrics_detect_low_energy(tmp_path):
    wav_path = tmp_path / "quiet.wav"
    _write_pcm_wav(wav_path, [40, -40] * 8000)

    metrics = analyze_wav(wav_path)

    assert metrics.duration_ms == 1000
    assert metrics.rms_dbfs < -50
    assert metrics.low_energy_ratio == 1.0
    assert metrics.clipping_ratio == 0.0


def test_asr_diagnosis_explains_short_hash_result(tmp_path):
    wav_path = tmp_path / "short.wav"
    _write_pcm_wav(wav_path, [2000, -2000] * 7200)
    metrics = analyze_wav(wav_path)

    reasons = infer_no_transcript_reasons(
        endpoint_reason="end_silence",
        vad_duration_ms=1380,
        vad_speech_ms=500,
        speech_start_packet=0,
        trimmed_audio_ms=900,
        metrics=metrics,
    )

    assert reasons[:3] == [
        "effective_speech_too_short",
        "utterance_too_short_or_clipped",
        "possible_leading_edge_clipped",
    ]
