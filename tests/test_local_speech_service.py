from __future__ import annotations

import asyncio
import io
import sys
import wave
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_inference.speech_service import create_app, pcm16_wave, wave_bytes  # noqa: E402


@pytest.fixture(autouse=True)
def run_worker_calls_inline(monkeypatch):
    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)


class FakeSpeechRuntime:
    def __init__(self):
        self.transcribed_bytes = 0
        self.spoken: list[tuple[str, str | None]] = []

    def transcribe(self, wav_data: bytes) -> str:
        self.transcribed_bytes = len(wav_data)
        return "本地语音识别成功"

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        self.spoken.append((text, voice))
        return wave_bytes([0.0, 0.5, -0.5], 16000)


def input_wave() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00\x00\x40\x00\xc0")
    return output.getvalue()


def asgi_request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def run() -> httpx.Response:
        encoded = httpx.Request(method, f"http://testserver{path}", **kwargs)
        body = encoded.read()
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(name.lower(), value) for name, value in encoded.headers.raw],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
        request = Request(scope, receive)
        route = next(route for route in app.routes if route.path == path)
        response = await route.endpoint(request)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.body,
        )

    return asyncio.run(run())


def test_pcm16_wave_round_trip():
    encoded = wave_bytes([0.0, 0.5, -0.5], 16000)
    samples, sample_rate = pcm16_wave(encoded)
    assert sample_rate == 16000
    assert samples[0] == 0.0
    assert 0.49 < samples[1] < 0.51
    assert -0.51 < samples[2] < -0.49


def test_openai_compatible_transcription_endpoint():
    runtime = FakeSpeechRuntime()
    response = asgi_request(
        create_app(runtime),
        "POST",
        "/v1/audio/transcriptions",
        data={"model": "SenseVoiceSmall", "language": "zh"},
        files={"file": ("speech.wav", input_wave(), "audio/wav")},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "本地语音识别成功"
    assert runtime.transcribed_bytes > 44


def test_openai_compatible_speech_endpoint_returns_wav():
    runtime = FakeSpeechRuntime()
    response = asgi_request(
        create_app(runtime),
        "POST",
        "/v1/audio/speech",
        json={
            "model": "vits-melo-tts-zh_en",
            "voice": "default",
            "input": "你好，StackChan。",
            "response_format": "wav",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF")
    assert runtime.spoken == [("你好，StackChan。", "default")]


def test_speech_endpoint_rejects_unsupported_audio_format():
    runtime = FakeSpeechRuntime()
    response = asgi_request(
        create_app(runtime),
        "POST",
        "/v1/audio/speech",
        json={"input": "hello", "response_format": "mp3"},
    )
    assert response.status_code == 400
    assert "response_format=wav" in response.json()["error"]["message"]


def test_speech_endpoint_reports_runtime_failure_as_server_error():
    class BrokenRuntime(FakeSpeechRuntime):
        def synthesize(self, text: str, voice: str | None = None) -> bytes:
            raise RuntimeError("inference failed")

    response = asgi_request(
        create_app(BrokenRuntime()),
        "POST",
        "/v1/audio/speech",
        json={"input": "hello", "response_format": "wav"},
    )
    assert response.status_code == 500
    assert "inference failed" in response.json()["error"]["message"]


def test_optional_bearer_auth(monkeypatch):
    monkeypatch.setenv("STACKCHAN_LOCAL_SPEECH_TOKEN", "offline-test-token")
    app = create_app(FakeSpeechRuntime())
    denied = asgi_request(app, "GET", "/v1/models")
    allowed = asgi_request(
        app,
        "GET",
        "/v1/models",
        headers={"Authorization": "Bearer offline-test-token"},
    )
    assert denied.status_code == 401
    assert allowed.status_code == 200
