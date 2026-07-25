#!/usr/bin/env python3
import asyncio
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-nanobot" / "bin" / "python"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_executable(path: Path, content: str):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _wait_health(port: int):
    deadline = time.time() + 8
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    return data
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.1)
    raise RuntimeError(f"bridge did not become healthy: {last_error}")


def _post_ota(port: int) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/xiaozhi/ota/",
        data=b'{"version":2}',
        headers={"Content-Type": "application/json", "Device-Id": "test", "Client-Id": "test-client"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def _exercise_ws(port: int):
    async def respond_to_mcp(ws, message: dict) -> bool:
        if message.get("type") != "mcp" or not isinstance(message.get("payload"), dict):
            return False
        payload = message["payload"]
        request_id = payload.get("id")
        method = payload.get("method")
        if request_id is None or not method:
            return False
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-stackchan", "version": "test"},
            }
        elif method == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        await ws.send(json.dumps({
            "type": "mcp",
            "payload": {"jsonrpc": "2.0", "id": request_id, "result": result},
        }))
        return True

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": 3,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }))
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello"
        connected_status = None
        while True:
            message = await asyncio.wait_for(ws.recv(), timeout=2)
            if not isinstance(message, bytes):
                parsed = json.loads(message)
                if await respond_to_mcp(ws, parsed):
                    continue
                if (
                    parsed.get("type") == "tts"
                    and parsed.get("state") == "sentence_start"
                    and "Bridge 已连接" in parsed.get("text", "")
                ):
                    connected_status = parsed
                    break
        assert connected_status is not None

        await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
        opus = b"fake-opus-frame"
        await ws.send(bytes([0, 0]) + struct.pack("!H", len(opus)) + opus)
        await ws.send(json.dumps({"type": "listen", "state": "stop"}))

        texts = []
        binaries = []
        ready = False
        deadline = time.time() + 10
        while time.time() < deadline and (len(binaries) < 2 or not ready):
            message = await asyncio.wait_for(ws.recv(), timeout=2)
            if isinstance(message, bytes):
                binaries.append(message)
            else:
                parsed = json.loads(message)
                if await respond_to_mcp(ws, parsed):
                    continue
                texts.append(parsed)
                ready = ready or (
                    parsed.get("type") == "tts"
                    and parsed.get("state") == "sentence_start"
                    and parsed.get("text") == "Nanobot 已就绪"
                )

        assert any(msg.get("type") == "stt" and msg.get("text") == "测试转写" for msg in texts), texts
        assert any(
            msg.get("type") == "tts" and msg.get("state") == "sentence_start"
            and msg.get("text") == "模型服务繁忙，请稍后再试。"
            for msg in texts
        ), texts
        assert len(binaries) == 2, binaries
        assert ready is True, texts
        assert binaries[0][:2] == b"\x00\x00", binaries
        assert struct.unpack("!H", binaries[0][2:4])[0] == 3, [b.hex() for b in binaries]
        assert binaries[0][4:] == b"abc", [b.hex() for b in binaries]
        assert struct.unpack("!H", binaries[1][2:4])[0] == 2, [b.hex() for b in binaries]
        assert binaries[1][4:] == b"de", [b.hex() for b in binaries]


def main() -> int:
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="stackchan-bridge-test-") as tmp_dir:
        tmp = Path(tmp_dir)
        asr = tmp / "fake_asr.sh"
        tts = tmp / "fake_tts.sh"
        _write_executable(asr, "#!/usr/bin/env bash\nprintf '测试转写\\n'\n")
        _write_executable(tts, "#!/usr/bin/env bash\nprintf '\\x00\\x03abc\\x00\\x02de'\n")

        env = os.environ.copy()
        env["STACKCHAN_ASR_COMMAND"] = str(asr)
        env["STACKCHAN_TTS_COMMAND"] = str(tts)
        env["STACKCHAN_TTS_SEGMENT_MAX_CHARS"] = "1000"
        env["STACKCHAN_REQUIRE_WAKE_WORD"] = "0"
        env["OPENAI_API_KEY"] = "offline-test-key"
        env["OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"
        env["STACKCHAN_CHAT_TIMEOUT"] = "1"
        env["STACKCHAN_CHAT_MAX_RETRIES"] = "0"
        proc = subprocess.Popen(
            [
                str(PYTHON),
                "-u",
                str(ROOT / "nanobot_bridge" / "server.py"),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--public-host",
                "127.0.0.1",
                "--nanobot-config",
                str(ROOT / "nanobot_config" / "config.json"),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            health = _wait_health(port)
            assert health["asr_configured"] is True
            assert health["tts_configured"] is True
            ota = _post_ota(port)
            assert ota["websocket"]["url"] == f"ws://127.0.0.1:{port}/ws"
            assert ota["websocket"]["version"] == 3
            asyncio.run(_exercise_ws(port))
        finally:
            proc.terminate()
            try:
                output, _ = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate(timeout=3)

        assert "[listen] stop frames=1" in output, output
        assert "[asr] complete transcript='测试转写'" in output, output
        assert "[tts] generated chars=" in output, output
        assert "packets=2 bytes=5" in output, output
        assert "Bad request version" not in output, output
    print("stackchan bridge protocol test ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
