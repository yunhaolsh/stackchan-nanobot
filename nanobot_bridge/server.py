#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import re
import selectors
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from asr_diagnostics import AudioSignalMetrics, analyze_wav, infer_no_transcript_reasons
from audio_endpoint import AudioEndpoint, EndpointDecision, trim_to_speech
from capabilities import DeviceCapabilityGateway, DeviceTool
from mcp_http import MCPHTTPService
from nanobot_runtime import NanobotRuntime


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class BridgeState:
    def __init__(
        self,
        host: str,
        port: int,
        public_host: str,
        token: str,
        nanobot_config: str | None,
        mcp_host: str,
        mcp_port: int,
    ):
        self.host = host
        self.port = port
        self.public_host = public_host
        self.token = token
        self.nanobot_config = nanobot_config
        self.asr_command = os.environ.get("STACKCHAN_ASR_COMMAND", "").strip()
        self.tts_command = os.environ.get("STACKCHAN_TTS_COMMAND", "").strip()
        self.vision_command = os.environ.get("STACKCHAN_VISION_COMMAND", "").strip()
        self.started_at = time.time()
        self.ws_clients: dict[socket.socket, ClientSession] = {}
        self.clients_lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stackchan-turn")
        self.vision_results_lock = threading.RLock()
        self.vision_results: dict[str, tuple[float, str]] = {}
        self.gateway = DeviceCapabilityGateway(
            rpc_timeout=float(os.environ.get("STACKCHAN_MCP_RPC_TIMEOUT", "20")),
            camera_rpc_timeout=float(os.environ.get("STACKCHAN_CAMERA_MCP_TIMEOUT", "120")),
            confirmation_ttl=float(os.environ.get("STACKCHAN_CONFIRMATION_TTL", "120")),
        )
        self.mcp_http = MCPHTTPService(self.gateway, host=mcp_host, port=mcp_port)
        self.nanobot = NanobotRuntime(nanobot_config, self.gateway)

    def remember_vision_result(self, device_key: str, answer: str) -> None:
        if not device_key or not answer:
            return
        with self.vision_results_lock:
            self.vision_results[device_key] = (time.monotonic(), answer)

    def latest_vision_result(self, device_key: str, *, not_before: float = 0.0) -> str:
        max_age = max(1.0, float(os.environ.get("STACKCHAN_VISION_RESULT_TTL", "120")))
        with self.vision_results_lock:
            result = self.vision_results.get(device_key)
        if (
            result is None
            or result[0] < not_before
            or time.monotonic() - result[0] > max_age
        ):
            return ""
        return result[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.public_host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.public_host}:{self.port}/ws"

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_http.host}:{self.mcp_http.port}/mcp"

    @property
    def vision_url(self) -> str:
        return f"{self.base_url}/vision/explain"

    def start(self) -> None:
        self.mcp_http.start()

    def stop(self) -> None:
        with self.clients_lock:
            sessions = list(self.ws_clients.values())
        for session in sessions:
            session.close("bridge stopping")
        self.nanobot.stop()
        self.mcp_http.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def register_client(self, session: "ClientSession") -> None:
        with self.clients_lock:
            replaced = [
                existing
                for existing in self.ws_clients.values()
                if existing.device_key == session.device_key and existing is not session
            ]
            for existing in replaced:
                self.ws_clients.pop(existing.sock, None)
            self.ws_clients[session.sock] = session
            client_count = len(self.ws_clients)
        for existing in replaced:
            print(
                f"[session] replacing device={session.device_key!r} "
                f"old={existing.session_id} new={session.session_id}"
            )
            existing.close("replaced by a newer device connection", shutdown=True)
        print(
            f"[session] registered device={session.device_key!r} "
            f"session={session.session_id} clients={client_count}"
        )
        if self.gateway.attach(session, []):
            self.nanobot.invalidate_tools()

    def unregister_client(self, session: "ClientSession") -> None:
        with self.clients_lock:
            self.ws_clients.pop(session.sock, None)
            client_count = len(self.ws_clients)
        session.close("device disconnected")
        print(
            f"[session] unregistered device={session.device_key!r} "
            f"session={session.session_id} clients={client_count}"
        )
        if self.gateway.detach(session):
            self.nanobot.invalidate_tools()

    def clients(self) -> list["ClientSession"]:
        with self.clients_lock:
            return list(self.ws_clients.values())

    def current_client(self, preferred: "ClientSession | None" = None) -> "ClientSession | None":
        """Return the newest live connection for the same physical device."""
        with self.clients_lock:
            clients = [session for session in self.ws_clients.values() if not session.closed]
            if preferred is not None and preferred.device_key:
                clients = [
                    session for session in clients
                    if session.device_key == preferred.device_key
                ]
            if not clients:
                return None
            return max(clients, key=lambda session: session.connected_at)

    def discover_device(self, session: "ClientSession") -> None:
        started_at = time.monotonic()
        try:
            initialize_params = {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "vision": {
                        "url": self.vision_url,
                        "token": self.token,
                    }
                },
                "clientInfo": {"name": "stackchan-nanobot-bridge", "version": "1.0.0"},
            }
            initialize_timeout = max(
                5.0,
                float(os.environ.get("STACKCHAN_MCP_INITIALIZE_TIMEOUT", "15")),
            )
            initialize_attempts = max(
                1,
                int(os.environ.get("STACKCHAN_MCP_INITIALIZE_ATTEMPTS", "2")),
            )
            initialized = None
            for attempt in range(1, initialize_attempts + 1):
                try:
                    initialized = session.rpc(
                        "initialize",
                        initialize_params,
                        timeout=initialize_timeout,
                    )
                    break
                except TimeoutError:
                    if self.current_client(session) is not session:
                        raise ConnectionError("device session was replaced during MCP initialize")
                    print(
                        f"[mcp] initialize timeout attempt={attempt}/{initialize_attempts} "
                        f"timeout_s={initialize_timeout:g} session={session.session_id}"
                    )
                    if attempt == initialize_attempts:
                        raise
                    session.send_json({
                        "type": "tts",
                        "state": "sentence_start",
                        "text": "设备响应较慢，正在自动重试",
                    })
            if initialized is None:
                raise RuntimeError("device MCP initialize returned no result")
            print(f"[mcp] initialized server={json.dumps(initialized, ensure_ascii=False)[:500]}")
            tools: list[DeviceTool] = []
            cursor = ""
            seen_cursors: set[str] = set()
            while True:
                params: dict[str, Any] = {"withUserTools": True}
                if cursor:
                    params["cursor"] = cursor
                page = session.rpc("tools/list", params, timeout=10)
                if not isinstance(page, dict):
                    raise RuntimeError("device tools/list returned a non-object result")
                for raw_tool in page.get("tools", []):
                    if isinstance(raw_tool, dict):
                        tools.append(DeviceTool.from_mcp(raw_tool))
                next_cursor = str(page.get("nextCursor") or "")
                if not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise RuntimeError(f"device tools/list cursor loop: {next_cursor}")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            if self.current_client(session) is not session:
                print(f"[mcp] discarding stale discovery session={session.session_id}")
                return
            if self.gateway.attach(session, tools):
                self.nanobot.invalidate_tools()
            session.mcp_ready = True
            model_tools = self.gateway.model_tools()
            denied = len(tools) - len(model_tools)
            ready_ms = int((time.monotonic() - started_at) * 1000)
            print(
                f"[mcp] discovered tools={len(tools)} model_visible={len(tools) - denied} "
                f"denied={denied} ready_ms={ready_ms}"
            )
            for tool in model_tools:
                permission = self.gateway.policy.classify(tool.name).value
                print(f"[mcp-tool] name={tool.name} permission={permission}")
            session.send_json({
                "type": "tts",
                "state": "sentence_start",
                "text": "Nanobot 已就绪",
            })
        except Exception as exc:
            print(
                f"[mcp] discovery failed elapsed_ms="
                f"{int((time.monotonic() - started_at) * 1000)}: {exc}"
            )
            if self.current_client(session) is session:
                try:
                    session.send_json({
                        "type": "tts",
                        "state": "sentence_start",
                        "text": "设备能力暂时不可用，请稍后再试",
                    })
                except ConnectionError:
                    pass


class ClientSession:
    def __init__(self, sock: socket.socket, device_key: str = ""):
        self.sock = sock
        self.device_key = device_key
        self.session_id = f"local-{time.time_ns()}"
        self.version = 3
        self.sample_rate = 16000
        self.frame_duration = 60
        self.listening = False
        self.listening_mode = "manual"
        self.frames: list[bytes] = []
        self.audio_frames_received = 0
        self.audio_endpoint: AudioEndpoint | None = None
        self.connected_at = time.time()
        self.listening_started_at = 0.0
        self.require_wake_word = os.environ.get("STACKCHAN_REQUIRE_WAKE_WORD", "0") == "1"
        self.wake_armed = not self.require_wake_word
        self.wake_detected_at = 0.0
        self.wake_guard_packets_remaining = 0
        self.wake_guard_packets_applied = 0
        self.send_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending: dict[int, Future[Any]] = {}
        self.next_rpc_id = 1
        self.tts_lock = threading.Lock()
        self.tts_generation = 0
        self.tts_active = False
        self.mcp_ready = False
        self.closed = False

    def begin_tts(self) -> int:
        with self.tts_lock:
            self.tts_generation += 1
            self.tts_active = True
            return self.tts_generation

    def cancel_tts(self, reason: str) -> bool:
        with self.tts_lock:
            was_active = self.tts_active
            if was_active:
                self.tts_generation += 1
                self.tts_active = False
                print(
                    f"[tts] cancel device={self.device_key!r} session={self.session_id} "
                    f"reason={reason!r}"
                )
            return was_active

    def is_tts_current(self, generation: int) -> bool:
        with self.tts_lock:
            return self.tts_active and self.tts_generation == generation and not self.closed

    def finish_tts(self, generation: int) -> None:
        with self.tts_lock:
            if self.tts_generation == generation:
                self.tts_active = False

    def start_listening(self, mode: str = "manual"):
        if not self.require_wake_word:
            self.wake_armed = True
        self.listening = True
        self.listening_mode = mode
        self.frames.clear()
        self.audio_frames_received = 0
        self.audio_endpoint = None
        wake_guard_ms = 0
        if time.monotonic() - self.wake_detected_at <= 3.0:
            wake_guard_ms = max(0, int(os.environ.get("STACKCHAN_WAKE_AUDIO_GUARD_MS", "720")))
        self.wake_guard_packets_remaining = (
            (wake_guard_ms + self.frame_duration - 1) // self.frame_duration
        )
        self.wake_guard_packets_applied = self.wake_guard_packets_remaining
        if mode == "auto":
            self.audio_endpoint = AudioEndpoint(
                sample_rate=self.sample_rate,
                packet_duration_ms=self.frame_duration,
                aggressiveness=int(os.environ.get("STACKCHAN_VAD_AGGRESSIVENESS", "3")),
                min_speech_ms=int(os.environ.get("STACKCHAN_VAD_MIN_SPEECH_MS", "300")),
                end_silence_ms=int(os.environ.get("STACKCHAN_VAD_END_SILENCE_MS", "600")),
                no_speech_timeout_ms=int(os.environ.get("STACKCHAN_VAD_NO_SPEECH_TIMEOUT_MS", "5000")),
                max_duration_ms=int(os.environ.get("STACKCHAN_VAD_MAX_DURATION_MS", "8000")),
            )
        self.listening_started_at = time.time()

    def arm_wake(self) -> None:
        # Drop any continuously uploaded pre-wake audio. The next listen/start
        # begins a clean command capture window.
        self.stop_listening()
        self.wake_detected_at = time.monotonic()
        self.wake_armed = True

    def consume_wake(self) -> None:
        if self.require_wake_word:
            self.wake_armed = False

    def stop_listening(self) -> list[bytes]:
        self.listening = False
        frames = self.frames
        self.frames = []
        self.audio_endpoint = None
        return frames

    def append_audio(self, opus_packet: bytes) -> EndpointDecision | None:
        if not self.listening:
            return None
        self.audio_frames_received += 1
        if not self.wake_armed:
            return None
        if self.wake_guard_packets_remaining > 0:
            self.wake_guard_packets_remaining -= 1
            return None
        self.frames.append(opus_packet)
        if self.audio_endpoint is None:
            return None
        return self.audio_endpoint.add_packet(opus_packet)

    def send_frame(self, opcode: int, payload: bytes) -> None:
        with self.send_lock:
            if self.closed:
                raise ConnectionError("device session is closed")
            _send_ws_frame(self.sock, opcode, payload)

    def send_json(self, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_frame(0x1, data)

    def rpc(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        with self.pending_lock:
            if self.closed:
                raise ConnectionError("device session is closed")
            request_id = self.next_rpc_id
            self.next_rpc_id += 1
            future: Future[Any] = Future()
            self.pending[request_id] = future
        started_at = time.monotonic()
        print(
            f"[mcp-rpc] send id={request_id} method={method} "
            f"timeout_s={timeout:g} session={self.session_id}"
        )
        try:
            self.send_json(
                {
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    },
                }
            )
            result = future.result(timeout=timeout)
            print(
                f"[mcp-rpc] complete id={request_id} method={method} "
                f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
            )
            return result
        except TimeoutError as exc:
            raise TimeoutError(f"device MCP request timed out: {method}") from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def resolve_rpc(self, payload: dict[str, Any]) -> bool:
        request_id = payload.get("id")
        if not isinstance(request_id, int):
            return False
        with self.pending_lock:
            future = self.pending.get(request_id)
        if future is None or future.done():
            return False
        error = payload.get("error")
        if error is not None:
            if isinstance(error, dict):
                message = str(error.get("message") or error)
            else:
                message = str(error)
            future.set_exception(RuntimeError(message))
        else:
            future.set_result(payload.get("result"))
        return True

    def close(self, reason: str, *, shutdown: bool = False) -> None:
        self.cancel_tts(reason)
        with self.pending_lock:
            if self.closed:
                return
            self.closed = True
            pending = list(self.pending.values())
            self.pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ConnectionError(reason))
        if shutdown:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict):
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _text_response(handler: BaseHTTPRequestHandler, status: int, body: str):
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    transfer_encoding = (handler.headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in transfer_encoding:
        chunks = bytearray()
        while True:
            size_line = handler.rfile.readline(128).strip().split(b";", 1)[0]
            if not size_line:
                continue
            size = int(size_line, 16)
            if size == 0:
                while handler.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                    pass
                break
            chunks.extend(handler.rfile.read(size))
            handler.rfile.read(2)
        return bytes(chunks)
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return b""
    return handler.rfile.read(length)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    data = _read_body(handler)
    if not data:
        return {}
    try:
        return json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        return {"_raw": data.decode("utf-8", errors="replace")}


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_ws_frame(sock: socket.socket):
    header = _recv_exact(sock, 2)
    b1, b2 = header
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _send_ws_frame(sock: socket.socket, opcode: int, payload: bytes):
    first = 0x80 | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", first, length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", first, 126, length)
    else:
        header = struct.pack("!BBQ", first, 127, length)
    sock.sendall(header + payload)


def _send_binary_audio(session: ClientSession, opus_packet: bytes):
    if session.version == 3:
        payload = bytes([0, 0]) + struct.pack("!H", len(opus_packet)) + opus_packet
    elif session.version == 2:
        payload = (
            struct.pack("!HHII", 2, 0, 0, len(opus_packet))
            + opus_packet
        )
    else:
        payload = opus_packet
    session.send_frame(0x2, payload)


def _stream_tts_packets(
    session: ClientSession,
    packets: list[bytes],
    *,
    interval_ms: float | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> int:
    """Send Opus near real time so the ESP32 decode queue cannot overflow."""
    if interval_ms is None:
        interval_ms = float(
            os.environ.get("STACKCHAN_TTS_PACKET_INTERVAL_MS", str(session.frame_duration))
        )
    clock = clock or time.monotonic
    sleeper = sleeper or time.sleep
    interval_seconds = max(0.0, interval_ms / 1000.0)
    deadline = clock()

    for packet in packets:
        _send_binary_audio(session, packet)
        if interval_seconds <= 0:
            continue
        deadline += interval_seconds
        remaining = deadline - clock()
        if remaining > 0:
            sleeper(remaining)
        elif remaining < -interval_seconds:
            # A stalled socket must not be followed by a catch-up burst.
            deadline = clock()
    return len(packets)


def _read_length_prefixed_packets(data: bytes) -> list[bytes]:
    packets = []
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("truncated tts packet length")
        size = struct.unpack("!H", data[offset:offset + 2])[0]
        offset += 2
        if offset + size > len(data):
            raise ValueError("truncated tts packet payload")
        packets.append(data[offset:offset + size])
        offset += size
    return packets


def _run_tts(state: BridgeState, session: ClientSession, text: str) -> list[bytes]:
    if not state.tts_command:
        return []
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="stackchan-tts-", suffix=".txt", delete=False) as tmp:
        input_path = tmp.name
        tmp.write(text)

    env = os.environ.copy()
    env.update({
        "STACKCHAN_TTS_INPUT": input_path,
        "STACKCHAN_TTS_SAMPLE_RATE": str(session.sample_rate),
        "STACKCHAN_TTS_FRAME_DURATION_MS": str(session.frame_duration),
    })
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            state.tts_command,
            shell=True,
            env=env,
            capture_output=True,
            timeout=120,
            check=False,
        )
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass

    if completed.returncode != 0:
        print(f"[tts] command failed rc={completed.returncode} stderr={completed.stderr[:500]!r}")
        return []
    packets = _read_length_prefixed_packets(completed.stdout)
    print(
        f"[tts] generated chars={len(text)} packets={len(packets)} "
        f"bytes={sum(len(p) for p in packets)} elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
    )
    return packets


def _stream_tts_command(
    state: BridgeState,
    session: ClientSession,
    text: str,
    generation: int | None = None,
) -> int:
    """Forward framed Opus from a progressive TTS adapter as it is produced."""
    if not state.tts_command:
        return 0
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="stackchan-tts-", suffix=".txt", delete=False) as tmp:
        input_path = tmp.name
        tmp.write(text)
    env = os.environ.copy()
    env.update({
        "STACKCHAN_TTS_INPUT": input_path,
        "STACKCHAN_TTS_SAMPLE_RATE": str(session.sample_rate),
        "STACKCHAN_TTS_FRAME_DURATION_MS": str(session.frame_duration),
    })
    started_at = time.monotonic()
    packet_count = 0
    first_packet_ms: int | None = None
    stderr_file = tempfile.TemporaryFile()
    process = subprocess.Popen(
        state.tts_command,
        shell=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
    )
    interval_seconds = max(
        0.0,
        float(os.environ.get("STACKCHAN_TTS_PACKET_INTERVAL_MS", str(session.frame_duration))) / 1000.0,
    )
    deadline = time.monotonic()
    first_packet_timeout = max(
        0.1, float(os.environ.get("STACKCHAN_TTS_FIRST_PACKET_TIMEOUT", "15"))
    )
    stream_idle_timeout = max(
        0.1, float(os.environ.get("STACKCHAN_TTS_STREAM_IDLE_TIMEOUT", "15"))
    )
    selector = selectors.DefaultSelector()
    try:
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        framed = bytearray()
        expected_size: int | None = None
        last_output_at = started_at
        reached_eof = False
        while True:
            if generation is not None and not session.is_tts_current(generation):
                process.kill()
                process.wait(timeout=5)
                print(
                    f"[tts] streaming canceled chars={len(text)} packets={packet_count} "
                    f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
                )
                return packet_count
            now = time.monotonic()
            timeout = first_packet_timeout if packet_count == 0 else stream_idle_timeout
            if now - last_output_at > timeout:
                phase = "first packet" if packet_count == 0 else "stream idle"
                raise TimeoutError(f"TTS {phase} timeout after {timeout:g}s")

            events = selector.select(timeout=0.1)
            if events:
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    framed.extend(chunk)
                    last_output_at = time.monotonic()
                elif process.poll() is not None:
                    reached_eof = True
            elif process.poll() is not None:
                reached_eof = True

            while True:
                if expected_size is None:
                    if len(framed) < 2:
                        break
                    expected_size = struct.unpack("!H", framed[:2])[0]
                    del framed[:2]
                if len(framed) < expected_size:
                    break
                payload = bytes(framed[:expected_size])
                del framed[:expected_size]
                expected_size = None
                if first_packet_ms is None:
                    first_packet_ms = int((time.monotonic() - started_at) * 1000)
                    print(f"[tts] first packet chars={len(text)} elapsed_ms={first_packet_ms}")
                if generation is not None and not session.is_tts_current(generation):
                    process.kill()
                    process.wait(timeout=5)
                    print(
                        f"[tts] streaming canceled before send chars={len(text)} packets={packet_count}"
                    )
                    return packet_count
                _send_binary_audio(session, payload)
                packet_count += 1
                if interval_seconds > 0:
                    deadline += interval_seconds
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                    elif remaining < -interval_seconds:
                        deadline = time.monotonic()

            if reached_eof:
                break
        if framed or expected_size is not None:
            raise ValueError("truncated streaming tts packet")
        return_code = process.wait(timeout=10)
        stderr_file.seek(0)
        stderr = stderr_file.read().decode("utf-8", errors="replace")
        if return_code != 0:
            print(f"[tts] streaming command failed rc={return_code} stderr={stderr[:500]!r}")
            return 0
        print(
            f"[tts] streamed chars={len(text)} packets={packet_count} "
            f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
        )
        return packet_count
    except Exception as exc:
        process.kill()
        process.wait(timeout=5)
        print(f"[tts] streaming command error: {exc}")
        return 0
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        stderr_file.close()
        try:
            os.unlink(input_path)
        except OSError:
            pass


def _split_tts_text(text: str, max_chars: int | None = None) -> list[str]:
    """Split replies into subtitle-sized utterances without dropping content."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if max_chars is None:
        max_chars = int(os.environ.get("STACKCHAN_TTS_SEGMENT_MAX_CHARS", "42"))
    max_chars = max(12, max_chars)
    hard_stops = "。！？!?；;\n"
    soft_stops = "，,、：: "
    segments: list[str] = []
    start = 0
    while start < len(text):
        limit = min(len(text), start + max_chars)
        end = limit
        if limit < len(text):
            hard = max((text.rfind(mark, start, limit + 1) for mark in hard_stops), default=-1)
            soft = max((text.rfind(mark, start, limit + 1) for mark in soft_stops), default=-1)
            split_at = hard if hard >= start + 8 else soft
            if split_at >= start + 8:
                end = split_at + 1
        segment = text[start:end].strip()
        if segment:
            segments.append(segment)
        start = end
    return segments


def _parse_multipart(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], dict[str, bytes]]:
    content_type = handler.headers.get("Content-Type") or ""
    if "multipart/form-data" not in content_type:
        raise ValueError("expected multipart/form-data")
    raw = _read_body(handler)
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + raw
    )
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            files[name] = payload
        else:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return fields, files


def _run_vision(state: BridgeState, image: bytes, question: str) -> str:
    if not state.vision_command:
        raise RuntimeError("STACKCHAN_VISION_COMMAND is not configured")
    with tempfile.NamedTemporaryFile("wb", prefix="stackchan-camera-", suffix=".jpg", delete=False) as tmp:
        image_path = tmp.name
        tmp.write(image)
    env = os.environ.copy()
    env.update(
        {
            "STACKCHAN_VISION_IMAGE": image_path,
            "STACKCHAN_VISION_QUESTION": question,
        }
    )
    try:
        completed = subprocess.run(
            state.vision_command,
            shell=True,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    finally:
        Path(image_path).unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError(f"vision command failed: {completed.stderr[:500]}")
    answer = completed.stdout.strip()
    if not answer:
        raise RuntimeError("vision command returned an empty response")
    print(f"[vision] bytes={len(image)} answer={answer[:300]!r}")
    return answer


def _normalize_vision_reply(answer: str) -> str:
    answer = re.sub(r"[*_`#]+", "", str(answer or ""))
    return re.sub(r"\s+", " ", answer).strip()


def _send_assistant_response(state: BridgeState, session: ClientSession, text: str):
    text = _sanitize_assistant_reply(text)
    segments = _split_tts_text(text)
    spoken_text = " ".join(segments)
    started_at = time.monotonic()
    generation = session.begin_tts()
    print(
        f"[tts] response start device={session.device_key!r} session={session.session_id} "
        f"chars={len(text)} segments={len(segments)}"
    )
    packet_count = 0
    if os.environ.get("STACKCHAN_TTS_COMMAND_STREAMING", "0") == "1":
        session.send_json({"type": "tts", "state": "start"})
        session.send_json({"type": "llm", "emotion": "happy"})
        if spoken_text:
            # One progressive request avoids a new cloud-TTS startup pause at
            # every subtitle boundary. The avatar can wrap the complete text.
            print(f"[tts] continuous text={spoken_text[:160]!r}")
            session.send_json({"type": "tts", "state": "sentence_start", "text": spoken_text})
            packet_count = _stream_tts_command(
                state,
                session,
                spoken_text,
                generation=generation,
            )
        if not session.is_tts_current(generation):
            print(
                f"[tts] response canceled device={session.device_key!r} "
                f"session={session.session_id} packets={packet_count}"
            )
            return
        if packet_count:
            time.sleep(max(0.0, float(os.environ.get("STACKCHAN_TTS_STOP_GRACE_MS", "300")) / 1000.0))
        if not session.is_tts_current(generation):
            return
        session.send_json({"type": "tts", "state": "stop"})
        session.finish_tts(generation)
        print(
            f"[tts] response complete device={session.device_key!r} session={session.session_id} "
            f"packets={packet_count} elapsed_ms={int((time.monotonic() - started_at) * 1000)} streaming=true"
        )
        return
    workers = min(3, max(1, len(segments)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stackchan-tts") as executor:
        pending_packets = [
            executor.submit(_run_tts, state, session, segment)
            for segment in segments
        ]
        if segments:
            # Keep the device idle while the first utterance is synthesized.
            pending_packets[0].result()
        session.send_json({"type": "tts", "state": "start"})
        session.send_json({"type": "llm", "emotion": "happy"})
        for index, (segment, future) in enumerate(zip(segments, pending_packets), start=1):
            packets = future.result()
            print(f"[tts] segment={index}/{len(segments)} text={segment[:80]!r}")
            session.send_json({"type": "tts", "state": "sentence_start", "text": segment})
            packet_count += _stream_tts_packets(session, packets)
    if packet_count:
        time.sleep(max(0.0, float(os.environ.get("STACKCHAN_TTS_STOP_GRACE_MS", "300")) / 1000.0))
    session.send_json({"type": "tts", "state": "stop"})
    session.finish_tts(generation)
    print(
        f"[tts] response complete device={session.device_key!r} session={session.session_id} "
        f"packets={packet_count} elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
    )


def _finish_without_reply(state: BridgeState, session: ClientSession) -> None:
    target = state.current_client(session)
    if target is None:
        return
    target.send_json({"type": "tts", "state": "start"})
    target.send_json({"type": "tts", "state": "stop"})


def _parse_binary_audio(payload: bytes, version: int) -> bytes:
    if version == 3:
        if len(payload) < 4:
            return b""
        payload_size = struct.unpack("!H", payload[2:4])[0]
        if payload_size > len(payload) - 4:
            print(f"[ws] invalid v3 audio payload size={payload_size} frame_bytes={len(payload)}")
            return b""
        return payload[4:4 + payload_size]
    if version == 2:
        if len(payload) < 12:
            return b""
        payload_size = struct.unpack("!I", payload[8:12])[0]
        if payload_size > len(payload) - 12:
            print(f"[ws] invalid v2 audio payload size={payload_size} frame_bytes={len(payload)}")
            return b""
        return payload[12:12 + payload_size]
    return payload


def _handle_audio_frame(state: BridgeState, session: ClientSession, payload: bytes):
    opus = _parse_binary_audio(payload, session.version)
    if not opus:
        return
    decision = session.append_audio(opus)
    if not session.listening:
        return
    if session.audio_frames_received <= 3 or session.audio_frames_received % 50 == 0:
        print(
            f"[ws] audio frame bytes={len(opus)} listening=True "
            f"buffered={len(session.frames)}"
        )
    if decision and decision.error and session.audio_endpoint and session.audio_endpoint.decode_errors == 1:
        print(f"[vad] opus decode failed: {decision.error}")
    if decision and decision.complete:
        raw_frames = session.stop_listening()
        session.consume_wake()
        frames = trim_to_speech(
            raw_frames,
            decision,
            packet_duration_ms=session.frame_duration,
            pre_roll_ms=int(os.environ.get("STACKCHAN_VAD_PRE_ROLL_MS", "240")),
            post_roll_ms=int(os.environ.get("STACKCHAN_VAD_POST_ROLL_MS", "300")),
        )
        print(
            f"[vad] endpoint reason={decision.reason} raw_frames={len(raw_frames)} "
            f"trimmed_frames={len(frames)} speech_packets="
            f"{decision.speech_start_packet}:{decision.speech_end_packet} "
            f"duration_ms={decision.duration_ms} speech_ms={decision.speech_ms} "
            f"silence_ms={decision.silence_ms}"
        )
        state.executor.submit(
            _process_audio_turn,
            state,
            session,
            frames,
            decision.reason,
            decision,
        )


def _run_nanobot_once(state: BridgeState, prompt: str, session_key: str = "stackchan:device") -> str:
    started_at = time.monotonic()
    print(f"[nanobot] request session_key={session_key!r} chars={len(prompt)}")
    try:
        result = state.nanobot.run(prompt, session_key=session_key)
        print(
            f"[nanobot] complete session_key={session_key!r} tools_used={result.tools_used} "
            f"reply_chars={len(result.content)} elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
        )
        reply = result.content.strip()
        if _is_provider_error_reply(reply):
            print(f"[nanobot] provider error sanitized content={reply[:160]!r}")
            return "模型服务繁忙，请稍后再试。"
        if _contains_tool_markup(reply):
            print(
                f"[nanobot] malformed tool markup sanitized "
                f"tools_used={result.tools_used!r} content={reply[:200]!r}"
            )
            return "模型没有正确调用设备能力，请再说一次。"
        return reply
    except Exception as exc:
        print(
            f"[nanobot] failed session_key={session_key!r} "
            f"elapsed_ms={int((time.monotonic() - started_at) * 1000)} error={exc}"
        )
        if "approved StackChan tools" in str(exc) or "not connected" in str(exc):
            return "设备正在重新连接，请稍后再试。"
        return "模型服务繁忙，请稍后再试。"


def _broadcast_assistant_text(state: BridgeState, text: str) -> int:
    sent = 0
    for session in state.clients():
        try:
            _send_assistant_response(state, session, text)
            sent += 1
        except (OSError, ConnectionError):
            state.unregister_client(session)
    return sent


def _run_asr(
    state: BridgeState,
    session: ClientSession,
    frames: list[bytes],
    endpoint_decision: EndpointDecision | None = None,
) -> str:
    if not frames:
        return ""
    if not state.asr_command:
        print(f"[asr] received {len(frames)} opus frames, but STACKCHAN_ASR_COMMAND is not configured")
        return ""

    started_at = time.monotonic()
    audio_ms = len(frames) * session.frame_duration
    print(
        f"[asr] start device={session.device_key!r} session={session.session_id} "
        f"frames={len(frames)} audio_ms={audio_ms}"
    )
    with tempfile.NamedTemporaryFile("wb", prefix="stackchan-opus-", suffix=".bin", delete=False) as tmp:
        path = tmp.name
        for frame in frames:
            tmp.write(struct.pack("!H", len(frame)))
            tmp.write(frame)

    env = os.environ.copy()
    env.update({
        "STACKCHAN_AUDIO_FRAMES": path,
        "STACKCHAN_AUDIO_FORMAT": "length_prefixed_opus",
        "STACKCHAN_AUDIO_SAMPLE_RATE": str(session.sample_rate),
        "STACKCHAN_AUDIO_FRAME_DURATION_MS": str(session.frame_duration),
        "STACKCHAN_ASR_DEBUG_ID": (
            f"{int(time.time() * 1000)}-{session.device_key.replace(':', '')}"
        ),
    })
    try:
        completed = subprocess.run(
            state.asr_command,
            shell=True,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if completed.returncode != 0:
        print(f"[asr] command failed rc={completed.returncode} stderr={completed.stderr[:500]!r}")
        return ""
    debug_wav = ""
    for line in completed.stderr.splitlines():
        if line.startswith("debug_wav="):
            debug_wav = line.partition("=")[2].strip()
            print(f"[asr] {line}")
        elif line.startswith("provider_result="):
            print(f"[asr-provider] {line.partition('=')[2]}")
    transcript = completed.stdout.strip()
    metrics: AudioSignalMetrics | None = None
    if debug_wav:
        try:
            metrics = analyze_wav(debug_wav)
            print(
                f"[asr-audio] wav={debug_wav!r} duration_ms={metrics.duration_ms} "
                f"sample_rate={metrics.sample_rate} rms_dbfs={metrics.rms_dbfs:.1f} "
                f"peak_dbfs={metrics.peak_dbfs:.1f} clipping_ratio={metrics.clipping_ratio:.5f} "
                f"low_energy_ratio={metrics.low_energy_ratio:.3f}"
            )
        except Exception as exc:
            print(f"[asr-audio] analysis_failed wav={debug_wav!r} error={exc}")
    print(
        f"[asr] complete transcript={transcript[:500]!r} "
        f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
    )
    if not _is_meaningful_transcript(transcript):
        decision = endpoint_decision or EndpointDecision(
            complete=True,
            reason="client_stop",
            duration_ms=audio_ms,
        )
        reasons = infer_no_transcript_reasons(
            endpoint_reason=decision.reason,
            vad_duration_ms=decision.duration_ms,
            vad_speech_ms=decision.speech_ms,
            speech_start_packet=decision.speech_start_packet,
            trimmed_audio_ms=audio_ms,
            metrics=metrics,
        )
        marker = "provider_no_transcript_marker" if transcript == "#" else "empty_or_non_speech"
        print(
            f"[asr-diagnosis] status=inferred marker={marker} reasons={','.join(reasons)} "
            f"endpoint={decision.reason or 'unknown'} vad_duration_ms={decision.duration_ms} "
            f"vad_speech_ms={decision.speech_ms} vad_silence_ms={decision.silence_ms} "
            f"speech_packets={decision.speech_start_packet}:{decision.speech_end_packet} "
            f"trimmed_audio_ms={audio_ms} wake_guard_ms="
            f"{session.wake_guard_packets_applied * session.frame_duration}"
        )
    return transcript


_CONFIRM_WORDS = {"确认", "我确认", "确认执行", "可以", "同意", "yes", "confirm"}
_CANCEL_WORDS = {"取消", "不确认", "不要", "算了", "no", "cancel"}


def _is_meaningful_transcript(transcript: str) -> bool:
    return any(character.isalnum() for character in transcript)


def _is_ambiguous_short_transcript(transcript: str) -> bool:
    normalized = transcript.strip().lower().rstrip("。.!！")
    return re.fullmatch(r"\d{1,4}", normalized) is not None


def _is_provider_error_reply(reply: str) -> bool:
    normalized = reply.strip().lower()
    return normalized.startswith((
        "error:",
        "error calling llm:",
        "nanobot request failed:",
        "[assistant reply unavailable due to model error.]",
    )) or (
        "1305" in normalized and "message" in normalized
    ) or (
        "request timed out" in normalized
    )


_TOOL_MARKUP_RE = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_calls|function_call|arg_key|arg_value)\b",
    re.IGNORECASE,
)


def _contains_tool_markup(reply: str) -> bool:
    return _TOOL_MARKUP_RE.search(reply) is not None


def _sanitize_assistant_reply(reply: str) -> str:
    reply = str(reply or "").strip()
    if _is_provider_error_reply(reply):
        return "模型服务繁忙，请稍后再试。"
    if _contains_tool_markup(reply):
        return "模型没有正确调用设备能力，请再说一次。"
    if not reply:
        return "没有生成有效回复，请再说一次。"
    return reply


def _reply_to_transcript(state: BridgeState, session: ClientSession, transcript: str) -> str:
    target = state.current_client(session)
    if target is None:
        return ""
    if target is not session:
        print(
            f"[turn] migrated input device={session.device_key!r} "
            f"from={session.session_id} to={target.session_id}"
        )
    target.send_json({"type": "stt", "text": transcript})
    normalized = transcript.strip().lower().rstrip("。.!！")
    pending_id = state.gateway.latest_pending_id(target.session_id)
    namespace = os.environ.get("STACKCHAN_SESSION_NAMESPACE", "stackchan").strip() or "stackchan"
    stable_session_key = f"{namespace}:{session.device_key or target.device_key or 'device'}"
    if pending_id and normalized in _CONFIRM_WORDS:
        pending_action = next(
            (
                action
                for action in state.gateway.pending_actions()
                if action.get("id") == pending_id
            ),
            {},
        )
        pending_tool = str(pending_action.get("tool") or "")
        operation_started_at = time.monotonic()
        try:
            tool_result = state.gateway.confirm(pending_id)
            if "camera.take_photo" in pending_tool:
                reply = _normalize_vision_reply(
                    state.latest_vision_result(
                        target.device_key,
                        not_before=operation_started_at,
                    )
                )
                if not reply:
                    prompt = (
                        "用户已明确确认摄像头操作。设备执行结果为："
                        f"{state.gateway.format_result(tool_result)}。请直接描述画面，不要再次调用工具。"
                    )
                    reply = _run_nanobot_once(state, prompt, session_key=stable_session_key)
            else:
                prompt = (
                    "用户已明确确认上一项设备操作。权限层已执行，结果为："
                    f"{state.gateway.format_result(tool_result)}。请用一句中文向用户说明结果，不要再次调用工具。"
                )
                reply = _run_nanobot_once(state, prompt, session_key=stable_session_key)
        except Exception as exc:
            recovered = (
                _normalize_vision_reply(
                    state.latest_vision_result(
                        target.device_key,
                        not_before=operation_started_at,
                    )
                )
                if "camera.take_photo" in pending_tool
                else ""
            )
            if recovered:
                print(
                    f"[vision] recovered successful analysis after device MCP error: {exc}"
                )
                reply = recovered
            else:
                prompt = f"用户确认了设备操作，但执行失败：{exc}。请用一句中文说明失败原因。"
                reply = _run_nanobot_once(state, prompt, session_key=stable_session_key)
    elif pending_id and normalized in _CANCEL_WORDS:
        state.gateway.cancel(pending_id)
        reply = _run_nanobot_once(
            state,
            "用户取消了上一项需要确认的设备操作。请用一句中文确认已取消，不要调用工具。",
            session_key=stable_session_key,
        )
    else:
        reply = _run_nanobot_once(state, transcript, session_key=stable_session_key)
    target = state.current_client(session)
    if target is None:
        print(f"[turn] reply dropped because device is offline device={session.device_key!r}")
        return reply
    if target is not session:
        print(
            f"[turn] migrated reply device={session.device_key!r} "
            f"from={session.session_id} to={target.session_id}"
        )
    _send_assistant_response(state, target, reply)
    return reply


def _process_audio_turn(
    state: BridgeState,
    session: ClientSession,
    frames: list[bytes],
    endpoint_reason: str = "",
    endpoint_decision: EndpointDecision | None = None,
) -> None:
    try:
        if endpoint_reason == "no_speech_timeout":
            print("[asr] no speech detected; returning to idle")
            _finish_without_reply(state, session)
            return
        transcript = _run_asr(state, session, frames, endpoint_decision)
        if _is_ambiguous_short_transcript(transcript):
            print(f"[asr] ambiguous short transcript={transcript!r}; rejecting turn")
            target = state.current_client(session)
            if target is not None:
                target.send_json({"type": "stt", "text": "未听清，请再说一次"})
            _finish_without_reply(state, session)
        elif _is_meaningful_transcript(transcript):
            _reply_to_transcript(state, session, transcript)
        else:
            print(f"[asr] empty or non-speech transcript={transcript[:80]!r}; returning to idle")
            _finish_without_reply(state, session)
    except Exception as exc:
        print(f"[turn] audio processing failed: {exc}")
        try:
            target = state.current_client(session)
            if target is not None:
                _send_assistant_response(state, target, f"语音处理失败：{exc}")
        except Exception:
            pass


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "StackChanNanobotBridge/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")

    @property
    def state(self) -> BridgeState:
        return self.server.bridge_state

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            device = self.state.gateway.health()
            _json_response(self, HTTPStatus.OK, {
                "ok": True,
                "uptime_sec": int(time.time() - self.state.started_at),
                "ws_url": self.state.ws_url,
                "mcp_url": self.state.mcp_url,
                "nanobot_config": self.state.nanobot_config or "",
                "ws_clients": len(self.state.clients()),
                "asr_configured": bool(self.state.asr_command),
                "tts_configured": bool(self.state.tts_command),
                "vision_configured": bool(self.state.vision_command),
                "device": device,
            })
            return
        if path == "/device/tools":
            _json_response(
                self,
                HTTPStatus.OK,
                {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "permission": self.state.gateway.policy.classify(tool.name).value,
                        }
                        for tool in self.state.gateway.all_tools()
                    ]
                },
            )
            return
        if path == "/permissions/pending":
            _json_response(self, HTTPStatus.OK, {"actions": self.state.gateway.pending_actions()})
            return
        if path == "/ws":
            self._handle_websocket()
            return
        if path == "/stackChan/apps":
            _json_response(self, HTTPStatus.OK, {"code": 0, "message": "ok", "data": []})
            return
        if path == "/stackChan/device/user":
            _json_response(self, HTTPStatus.OK, {"code": 0, "message": "ok", "data": {"bound": False}})
            return
        if path == "/stackChan/device/info":
            _json_response(self, HTTPStatus.OK, {"code": 0, "message": "ok", "data": {"name": "StackChan"}})
            return
        _text_response(self, HTTPStatus.OK, "StackChan Nanobot Bridge\n")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/vision/explain":
            authorization = self.headers.get("Authorization") or ""
            if self.state.token and authorization != f"Bearer {self.state.token}":
                _json_response(self, HTTPStatus.UNAUTHORIZED, {"success": False, "message": "unauthorized"})
                return
            try:
                fields, files = _parse_multipart(self)
                image = files.get("file") or b""
                if not image:
                    raise ValueError("multipart field 'file' is required")
                answer = _run_vision(
                    self.state,
                    image,
                    fields.get("question") or "请简洁描述你看到的内容。",
                )
                self.state.remember_vision_result(
                    self.headers.get("Device-Id") or self.client_address[0],
                    answer,
                )
                _json_response(self, HTTPStatus.OK, {"success": True, "message": answer})
            except Exception as exc:
                _json_response(self, HTTPStatus.BAD_GATEWAY, {"success": False, "message": str(exc)})
            return
        body = _read_json_body(self)
        if path in ("/xiaozhi/ota", "/xiaozhi/ota/"):
            request_started = time.monotonic()
            print(f"[ota] {self.client_address[0]} headers={{Device-Id:{self.headers.get('Device-Id')}, Client-Id:{self.headers.get('Client-Id')}}}")
            if body:
                print(f"[ota] body={json.dumps(body, ensure_ascii=False)[:1000]}")
            _json_response(self, HTTPStatus.OK, {
                "firmware": {},
                "websocket": {
                    "url": self.state.ws_url,
                    "token": self.state.token,
                    "version": 3,
                },
                "server_time": {
                    "timestamp": int(time.time() * 1000),
                    "timezone_offset": 480,
                },
            })
            print(f"[ota] response complete elapsed_ms={int((time.monotonic() - request_started) * 1000)}")
            return
        if path == "/stackChan/device/unbind":
            _json_response(self, HTTPStatus.OK, {"code": 0, "message": "ok", "data": {}})
            return
        if path == "/nanobot/message":
            text = str(body.get("text") or "").strip()
            if not text:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "missing text"})
                return
            reply = _run_nanobot_once(self.state, text)
            pushed = _broadcast_assistant_text(self.state, reply)
            _json_response(self, HTTPStatus.OK, {"reply": reply, "pushed_to_stackchan": pushed})
            return
        if path == "/stackchan/say":
            text = str(body.get("text") or "").strip()
            if not text:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "missing text"})
                return
            pushed = _broadcast_assistant_text(self.state, text)
            _json_response(self, HTTPStatus.OK, {"ok": pushed > 0, "pushed_to_stackchan": pushed})
            return
        if path == "/stackchan/transcript":
            text = str(body.get("text") or "").strip()
            if not text:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "missing text"})
                return
            replies = []
            for session in self.state.clients():
                try:
                    replies.append(_reply_to_transcript(self.state, session, text))
                except (OSError, ConnectionError):
                    self.state.unregister_client(session)
            _json_response(self, HTTPStatus.OK, {
                "ok": bool(replies),
                "transcript": text,
                "reply": replies[0] if replies else "",
                "pushed_to_stackchan": len(replies),
            })
            return
        if path.startswith("/permissions/"):
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3 and parts[0] == "permissions":
                action_id, operation = parts[1], parts[2]
                try:
                    if operation == "confirm":
                        result = self.state.gateway.confirm(action_id)
                        _json_response(self, HTTPStatus.OK, {"ok": True, "result": result})
                        return
                    if operation == "cancel":
                        cancelled = self.state.gateway.cancel(action_id)
                        _json_response(self, HTTPStatus.OK, {"ok": cancelled})
                        return
                except Exception as exc:
                    _json_response(self, HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found", "path": path})

    def _handle_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "missing websocket key"})
            return

        accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True

        sock = self.connection
        print(f"[ws] connected from {self.client_address[0]} auth={self.headers.get('Authorization')!r}")
        device_key = (self.headers.get("Device-Id") or self.client_address[0]).strip()
        session = ClientSession(sock, device_key=device_key)
        self.state.register_client(session)
        try:
            while True:
                opcode, payload = _recv_ws_frame(sock)
                if opcode == 0x8:
                    print("[ws] close")
                    break
                if opcode == 0x9:
                    session.send_frame(0xA, payload)
                    continue
                if opcode == 0x1:
                    text = payload.decode("utf-8", errors="replace")
                    print(f"[ws] text={text[:1000]}")
                    self._handle_ws_text(session, text)
                    continue
                if opcode == 0x2:
                    _handle_audio_frame(self.state, session, payload)
                    continue
                print(f"[ws] ignored opcode={opcode} bytes={len(payload)}")
        except ConnectionError as exc:
            print(f"[ws] disconnected session={session.session_id}: {exc}")
        except Exception as exc:
            print(f"[ws] error: {exc}")
        finally:
            self.state.unregister_client(session)

    def _handle_ws_text(self, session: ClientSession, text: str):
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        message_session_id = msg.get("session_id")
        if (
            msg_type != "hello"
            and isinstance(message_session_id, str)
            and message_session_id
            and message_session_id != session.session_id
        ):
            print(
                f"[ws] stale message ignored type={msg_type!r} "
                f"message_session={message_session_id!r} current_session={session.session_id!r}"
            )
            return
        if msg_type == "hello":
            version = msg.get("version")
            if isinstance(version, int):
                session.version = version
            audio_params = msg.get("audio_params") if isinstance(msg.get("audio_params"), dict) else {}
            if isinstance(audio_params.get("sample_rate"), int):
                session.sample_rate = audio_params["sample_rate"]
            if isinstance(audio_params.get("frame_duration"), int):
                session.frame_duration = audio_params["frame_duration"]
            session.send_json({
                "type": "hello",
                "transport": "websocket",
                "session_id": session.session_id,
                "audio_params": {
                    "format": "opus",
                    "sample_rate": session.sample_rate,
                    "channels": 1,
                    "frame_duration": session.frame_duration,
                },
            })
            session.send_json({
                "type": "tts",
                "state": "sentence_start",
                "text": "Bridge 已连接，正在加载设备能力",
            })
            print(
                f"[session] hello device={session.device_key!r} session={session.session_id} "
                f"version={session.version} sample_rate={session.sample_rate} "
                f"frame_duration={session.frame_duration}"
            )
            self.state.executor.submit(self.state.discover_device, session)
            return
        if msg_type == "mcp":
            payload = msg.get("payload")
            if isinstance(payload, dict) and session.resolve_rpc(payload):
                print(f"[mcp] response id={payload.get('id')}")
            else:
                print(f"[mcp] unhandled={json.dumps(payload or {}, ensure_ascii=False)[:1000]}")
            return
        if msg_type == "text":
            prompt = str(msg.get("text") or "").strip()
            if prompt:
                self.state.executor.submit(_reply_to_transcript, self.state, session, prompt)
            return
        if msg_type == "abort":
            reason = str(msg.get("reason") or "")
            session.cancel_tts(reason or "device_abort")
            if reason == "wake_word_detected":
                session.arm_wake()
                print(
                    f"[wake] armed from abort device={session.device_key!r} "
                    f"session={session.session_id}"
                )
            return
        if msg_type == "listen":
            listen_state = msg.get("state")
            if listen_state == "detect":
                session.cancel_tts("wake_word_detected")
                session.arm_wake()
                print(
                    f"[wake] detected device={session.device_key!r} "
                    f"session={session.session_id} text={str(msg.get('text') or '')!r}"
                )
                return
            if listen_state == "start":
                session.start_listening(str(msg.get("mode") or "manual"))
                print(
                    f"[listen] start mode={msg.get('mode')} session={session.session_id} "
                    f"wake_armed={session.wake_armed} "
                    f"wake_guard_packets={session.wake_guard_packets_remaining}"
                )
                return
            if listen_state == "stop":
                frames = session.stop_listening()
                session.consume_wake()
                print(f"[listen] stop frames={len(frames)} session={session.session_id}")
                self.state.executor.submit(_process_audio_turn, self.state, session, frames, "client_stop")
                return


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, request_handler_class, bridge_state):
        super().__init__(server_address, request_handler_class)
        self.bridge_state = bridge_state


def parse_args():
    parser = argparse.ArgumentParser(description="Local StackChan Nanobot bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=12800)
    parser.add_argument("--public-host", default=os.environ.get("STACKCHAN_PUBLIC_HOST", "127.0.0.1"))
    parser.add_argument("--token", default=os.environ.get("STACKCHAN_BRIDGE_TOKEN", "hi-stack-chan"))
    parser.add_argument("--nanobot-config", default=os.environ.get("NANOBOT_CONFIG"))
    parser.add_argument("--mcp-host", default=os.environ.get("STACKCHAN_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--mcp-port", type=int, default=int(os.environ.get("STACKCHAN_MCP_PORT", "12801")))
    return parser.parse_args()


def main():
    args = parse_args()
    state = BridgeState(
        args.host,
        args.port,
        args.public_host,
        args.token,
        args.nanobot_config,
        args.mcp_host,
        args.mcp_port,
    )
    state.start()
    server = BridgeHTTPServer((args.host, args.port), BridgeHandler, state)
    print(f"[bridge] HTTP: {state.base_url}")
    print(f"[bridge] OTA : {state.base_url}/xiaozhi/ota/")
    print(f"[bridge] WS  : {state.ws_url}")
    print(f"[bridge] MCP : {state.mcp_url}")

    def request_shutdown(*_args):
        threading.Thread(target=server.shutdown, name="stackchan-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        state.stop()


if __name__ == "__main__":
    main()
