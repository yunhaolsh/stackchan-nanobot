#!/usr/bin/env python3
"""OpenAI-compatible local ASR and TTS service backed by sherpa-onnx."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import io
import logging
import os
import threading
import time
import wave
from array import array
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ROOT = ROOT / "models" / "speech"
DEFAULT_ASR_DIR = DEFAULT_MODEL_ROOT / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
DEFAULT_TTS_DIR = DEFAULT_MODEL_ROOT / "vits-melo-tts-zh_en"
LOGGER = logging.getLogger("stackchan.local_speech")


def pcm16_wave(data: bytes) -> tuple[list[float], int]:
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("audio must be mono 16-bit PCM WAV")
        sample_rate = wav.getframerate()
        samples = array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
    if os.sys.byteorder != "little":
        samples.byteswap()
    return [sample / 32768.0 for sample in samples], sample_rate


def wave_bytes(samples: Any, sample_rate: int) -> bytes:
    pcm = array(
        "h",
        (
            max(-32768, min(32767, round(float(sample) * 32767.0)))
            for sample in samples
        ),
    )
    if os.sys.byteorder != "little":
        pcm.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


class SherpaSpeechRuntime:
    def __init__(
        self,
        *,
        asr_dir: Path,
        tts_dir: Path,
        provider: str = "cpu",
        num_threads: int = 4,
        language: str = "zh",
        use_itn: bool = True,
        default_sid: int = 0,
        speed: float = 1.0,
        sherpa_module: Any | None = None,
    ):
        if sherpa_module is None:
            import sherpa_onnx as sherpa_module

        self.sherpa = sherpa_module
        self.asr_dir = asr_dir
        self.tts_dir = tts_dir
        self.provider = provider
        self.num_threads = num_threads
        self.default_sid = default_sid
        self.speed = speed
        self._asr_lock = threading.Lock()
        self._tts_lock = threading.Lock()

        asr_model = asr_dir / "model.int8.onnx"
        asr_tokens = asr_dir / "tokens.txt"
        for path in (asr_model, asr_tokens):
            if not path.is_file():
                raise FileNotFoundError(path)
        self.recognizer = sherpa_module.OfflineRecognizer.from_sense_voice(
            model=str(asr_model),
            tokens=str(asr_tokens),
            num_threads=num_threads,
            provider=provider,
            language=language,
            use_itn=use_itn,
        )

        tts_model = tts_dir / "model.onnx"
        tts_lexicon = tts_dir / "lexicon.txt"
        tts_tokens = tts_dir / "tokens.txt"
        for path in (tts_model, tts_lexicon, tts_tokens):
            if not path.is_file():
                raise FileNotFoundError(path)
        rule_fsts = ",".join(
            str(path)
            for name in ("phone.fst", "date.fst", "number.fst")
            if (path := tts_dir / name).is_file()
        )
        tts_config = sherpa_module.OfflineTtsConfig(
            model=sherpa_module.OfflineTtsModelConfig(
                vits=sherpa_module.OfflineTtsVitsModelConfig(
                    model=str(tts_model),
                    lexicon=str(tts_lexicon),
                    tokens=str(tts_tokens),
                ),
                provider=provider,
                debug=False,
                num_threads=num_threads,
            ),
            rule_fsts=rule_fsts,
            max_num_sentences=1,
        )
        if not tts_config.validate():
            raise ValueError("invalid sherpa-onnx TTS configuration")
        self.tts = sherpa_module.OfflineTts(tts_config)

    @classmethod
    def from_env(cls) -> "SherpaSpeechRuntime":
        return cls(
            asr_dir=Path(os.environ.get("STACKCHAN_LOCAL_ASR_MODEL_DIR", DEFAULT_ASR_DIR)),
            tts_dir=Path(os.environ.get("STACKCHAN_LOCAL_TTS_MODEL_DIR", DEFAULT_TTS_DIR)),
            provider=os.environ.get("STACKCHAN_LOCAL_SPEECH_PROVIDER", "cpu"),
            num_threads=max(1, int(os.environ.get("STACKCHAN_LOCAL_SPEECH_THREADS", "4"))),
            language=os.environ.get("STACKCHAN_LOCAL_ASR_LANGUAGE", "zh"),
            use_itn=os.environ.get("STACKCHAN_LOCAL_ASR_USE_ITN", "1") == "1",
            default_sid=int(os.environ.get("STACKCHAN_LOCAL_TTS_SID", "0")),
            speed=float(os.environ.get("STACKCHAN_LOCAL_TTS_SPEED", "1.0")),
        )

    def transcribe(self, wav_data: bytes) -> str:
        samples, sample_rate = pcm16_wave(wav_data)
        with self._asr_lock:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            self.recognizer.decode_stream(stream)
            return (stream.result.text or "").strip()

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        sid = self.default_sid
        if voice and voice not in {"default", "alloy"}:
            try:
                sid = int(voice)
            except ValueError:
                pass
        generation = self.sherpa.GenerationConfig()
        generation.sid = sid
        generation.speed = self.speed
        generation.silence_scale = 0.2
        with self._tts_lock:
            audio = self.tts.generate(text, generation)
        if len(audio.samples) == 0:
            raise RuntimeError("sherpa-onnx generated no audio")
        return wave_bytes(audio.samples, audio.sample_rate)


def authorized(request: Request) -> bool:
    expected = os.environ.get("STACKCHAN_LOCAL_SPEECH_TOKEN", "")
    if not expected:
        return True
    return hmac.compare_digest(
        request.headers.get("authorization", ""),
        f"Bearer {expected}",
    )


def create_app(runtime: Any) -> Starlette:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "backend": "sherpa-onnx",
                "asr": "SenseVoiceSmall",
                "tts": "vits-melo-tts-zh_en",
            }
        )

    async def models(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": "SenseVoiceSmall", "object": "model", "owned_by": "local"},
                    {"id": "vits-melo-tts-zh_en", "object": "model", "owned_by": "local"},
                ],
            }
        )

    async def transcriptions(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        started = time.monotonic()
        try:
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise ValueError("multipart field 'file' is required")
            audio = await upload.read()
            text = await asyncio.to_thread(runtime.transcribe, audio)
            return JSONResponse(
                {
                    "text": text,
                    "model": str(form.get("model") or "SenseVoiceSmall"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            )
        except ValueError as exc:
            return JSONResponse({"error": {"message": str(exc)}}, status_code=400)
        except Exception as exc:
            LOGGER.exception("local ASR failed")
            return JSONResponse({"error": {"message": str(exc)}}, status_code=500)

    async def speech(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        started = time.monotonic()
        try:
            payload = await request.json()
            text = str(payload.get("input", "")).strip()
            if not text:
                raise ValueError("input text is required")
            response_format = str(payload.get("response_format", "wav"))
            if response_format != "wav":
                raise ValueError("local speech service supports response_format=wav")
            audio = await asyncio.to_thread(runtime.synthesize, text, payload.get("voice"))
            return Response(
                audio,
                media_type="audio/wav",
                headers={
                    "X-StackChan-Elapsed-Ms": str(round((time.monotonic() - started) * 1000))
                },
            )
        except ValueError as exc:
            return JSONResponse({"error": {"message": str(exc)}}, status_code=400)
        except Exception as exc:
            LOGGER.exception("local TTS failed")
            return JSONResponse({"error": {"message": str(exc)}}, status_code=500)

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/audio/transcriptions", transcriptions, methods=["POST"]),
            Route("/v1/audio/speech", speech, methods=["POST"]),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("STACKCHAN_LOCAL_SPEECH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("STACKCHAN_LOCAL_SPEECH_PORT", "18081")))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    started = time.monotonic()
    runtime = SherpaSpeechRuntime.from_env()
    print(f"[local-speech] models loaded elapsed_ms={round((time.monotonic() - started) * 1000)}")
    if args.check:
        return 0
    import uvicorn

    uvicorn.run(create_app(runtime), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
