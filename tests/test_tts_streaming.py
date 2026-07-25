from __future__ import annotations

import sys
import shlex
import struct
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "nanobot_bridge"))

import server  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from stackchan_audio import _ogg_page, iter_ogg_packets  # noqa: E402
from stackchan_tts_glm import _encode_pcm_chunks  # noqa: E402

import opuslib_next  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSession:
    version = 3
    frame_duration = 60

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.sent_at: list[float] = []
        self.payloads: list[bytes] = []

    def send_frame(self, opcode: int, payload: bytes):
        assert opcode == 0x2
        self.sent_at.append(self.clock.monotonic())
        self.payloads.append(payload)


class FakeResponseSession:
    device_key = "test-device"
    session_id = "test-session"

    def __init__(self):
        self.messages: list[dict] = []
        self.generation = 0
        self.active = False

    def begin_tts(self) -> int:
        self.generation += 1
        self.active = True
        return self.generation

    def is_tts_current(self, generation: int) -> bool:
        return self.active and generation == self.generation

    def finish_tts(self, generation: int) -> None:
        if generation == self.generation:
            self.active = False

    def send_json(self, body: dict):
        self.messages.append(body)


def test_tts_packets_are_paced_at_device_frame_duration():
    clock = FakeClock()
    session = FakeSession(clock)

    sent = server._stream_tts_packets(
        session,
        [b"one", b"two", b"three"],
        interval_ms=60,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert sent == 3
    assert session.sent_at == [0.0, 0.06, 0.12]
    assert len(clock.sleeps) == 3
    assert abs(sum(clock.sleeps) - 0.18) < 1e-9
    assert [payload[4:] for payload in session.payloads] == [b"one", b"two", b"three"]


def test_tts_stream_does_not_burst_after_a_stall():
    clock = FakeClock()
    session = FakeSession(clock)

    def send_frame(opcode: int, payload: bytes):
        session.sent_at.append(clock.monotonic())
        session.payloads.append(payload)
        if len(session.sent_at) == 1:
            clock.now += 0.2

    session.send_frame = send_frame
    server._stream_tts_packets(
        session,
        [b"one", b"two", b"three"],
        interval_ms=60,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert session.sent_at[0] == 0.0
    assert session.sent_at[1] == 0.2
    assert session.sent_at[2] >= 0.26


def test_tts_text_is_split_without_losing_content():
    text = "第一句话比较短。第二句话也需要完整显示！最后一段没有标点但仍然不能丢失"
    segments = server._split_tts_text(text, max_chars=16)

    assert len(segments) >= 3
    assert "".join(segments) == text
    assert all(len(segment) <= 16 for segment in segments)


def test_non_speech_asr_markers_are_rejected():
    assert server._is_meaningful_transcript("") is False
    assert server._is_meaningful_transcript("#") is False
    assert server._is_meaningful_transcript("……！？") is False
    assert server._is_meaningful_transcript("你好") is True
    assert server._is_meaningful_transcript("timer 5") is True


def test_digits_only_asr_is_treated_as_ambiguous():
    assert server._is_ambiguous_short_transcript("10") is True
    assert server._is_ambiguous_short_transcript(" 360。") is True
    assert server._is_ambiguous_short_transcript("向右转10度") is False
    assert server._is_ambiguous_short_transcript("十秒") is False


def test_provider_errors_are_not_spoken_verbatim():
    assert server._is_provider_error_reply("Error: {'code': '1305', 'message': 'busy'}") is True
    assert server._is_provider_error_reply("Nanobot request failed: timeout") is True
    assert server._is_provider_error_reply("Error calling LLM: Request timed out.") is True
    assert server._is_provider_error_reply("[Assistant reply unavailable due to model error.]") is True
    assert server._is_provider_error_reply("今天的天气很好") is False


@pytest.mark.parametrize(
    "reply",
    [
        "<tool_call>mcp_stackchan_self_robot_get_head_angles</tool_call>",
        "<tool_call>cron<arg_key>action</arg_key><arg_value>create</arg_value>",
        "<function_call>{\"name\":\"web_search\"}</function_call>",
    ],
)
def test_raw_tool_markup_is_never_sent_to_the_device(reply):
    assert server._contains_tool_markup(reply) is True
    assert server._sanitize_assistant_reply(reply) == "模型没有正确调用设备能力，请再说一次。"


def test_assistant_reply_sanitizer_keeps_normal_chinese_text():
    assert server._sanitize_assistant_reply("已启动20秒倒计时。") == "已启动20秒倒计时。"


def test_ogg_packets_can_be_read_from_a_progressive_stream():
    stream = BytesIO(
        _ogg_page(b"OpusHead", serial=7, sequence=0, granule=0, header_type=2)
        + _ogg_page(b"first", serial=7, sequence=1, granule=2880, header_type=0)
        + _ogg_page(b"second", serial=7, sequence=2, granule=5760, header_type=4)
    )

    assert list(iter_ogg_packets(stream)) == [b"OpusHead", b"first", b"second"]


def test_streaming_tts_command_forwards_packets_without_buffering(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_streaming_tts.py"
    script.write_text(
        "import struct, sys\n"
        "for packet in (b'first', b'second'):\n"
        "    sys.stdout.buffer.write(struct.pack('!H', len(packet)) + packet)\n"
        "    sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    state = SimpleNamespace(tts_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    clock = FakeClock()
    session = FakeSession(clock)
    session.sample_rate = 16000
    session.device_key = "test-device"
    session.session_id = "test-session"
    monkeypatch.setenv("STACKCHAN_TTS_PACKET_INTERVAL_MS", "0")

    packet_count = server._stream_tts_command(state, session, "测试")

    assert packet_count == 2
    assert [payload[4:] for payload in session.payloads] == [b"first", b"second"]


def test_streaming_tts_first_packet_watchdog_stops_a_stalled_provider(tmp_path: Path, monkeypatch):
    script = tmp_path / "stalled_tts.py"
    script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    state = SimpleNamespace(tts_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    session = FakeSession(FakeClock())
    session.sample_rate = 16000
    session.device_key = "test-device"
    session.session_id = "test-session"
    monkeypatch.setenv("STACKCHAN_TTS_FIRST_PACKET_TIMEOUT", "0.1")
    started_at = time.monotonic()

    packet_count = server._stream_tts_command(state, session, "测试")

    assert packet_count == 0
    assert time.monotonic() - started_at < 2


def test_vision_reply_is_normalized_for_direct_streaming_tts():
    answer = "人物拿着一部**黑色手机**。\n\n环境为_室内_。"

    assert server._normalize_vision_reply(answer) == "人物拿着一部黑色手机。 环境为室内。"


def test_long_streaming_reply_uses_one_continuous_tts_request(monkeypatch):
    session = FakeResponseSession()
    calls: list[str] = []
    text = "第一段用于说明。第二段仍应属于同一次语音合成请求。第三段不能再次启动云端请求。"
    monkeypatch.setenv("STACKCHAN_TTS_COMMAND_STREAMING", "1")
    monkeypatch.setenv("STACKCHAN_TTS_STOP_GRACE_MS", "0")

    def stream(_state, _session, spoken_text, generation=None):
        assert _session.is_tts_current(generation)
        calls.append(spoken_text)
        return 3

    monkeypatch.setattr(server, "_stream_tts_command", stream)

    server._send_assistant_response(SimpleNamespace(), session, text)

    assert calls == [text]
    subtitles = [message for message in session.messages if message.get("state") == "sentence_start"]
    assert subtitles == [{"type": "tts", "state": "sentence_start", "text": text}]
    assert session.messages[0] == {"type": "tts", "state": "start"}
    assert session.messages[-1] == {"type": "tts", "state": "stop"}
    assert session.active is False


def test_glm_pcm_chunks_are_encoded_without_waiting_for_complete_audio():
    packets = _encode_pcm_chunks(iter([bytes(2880), bytes(1440), bytes(1440)]), 24000, 60)
    first = next(packets)
    remaining = list(packets)

    assert first
    assert len(remaining) == 1
    decoder = opuslib_next.Decoder(16000, 1)
    decoded = decoder.decode(first, 960)
    assert len(decoded) == 960 * 2
