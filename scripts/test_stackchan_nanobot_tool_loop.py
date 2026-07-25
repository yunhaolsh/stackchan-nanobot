#!/usr/bin/env python3
"""Prove Nanobot owns the model/tool loop and calls StackChan through MCP."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-nanobot" / "bin" / "python"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class FakeLLMState:
    def __init__(self):
        self.requests: list[dict] = []


class FakeLLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.state.requests.append(body)
        has_tool_result = any(message.get("role") == "tool" for message in body.get("messages", []))
        if has_tool_result:
            message = {"role": "assistant", "content": "厨房计时器已开始。"}
            finish_reason = "stop"
        else:
            names = {
                tool.get("function", {}).get("name")
                for tool in body.get("tools", [])
                if isinstance(tool, dict)
            }
            expected = "mcp_stackchan_self_timer_start"
            if expected not in names:
                message = {"role": "assistant", "content": f"missing tool: {sorted(names)}"}
                finish_reason = "stop"
            else:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_timer_1",
                            "type": "function",
                            "function": {
                                "name": expected,
                                "arguments": json.dumps(
                                    {
                                        "name": "厨房",
                                        "duration_seconds": 60,
                                        "message": "厨房计时结束",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                }
                finish_reason = "tool_calls"
        response = {
            "id": f"chatcmpl-{len(self.server.state.requests)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "glm-4.7-flash",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        data = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def wait_for_tools(bridge_port: int):
    deadline = time.time() + 8
    while time.time() < deadline:
        health = await asyncio.to_thread(get_json, f"http://127.0.0.1:{bridge_port}/health")
        if health.get("device", {}).get("tool_count") == 2:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("device tools were not discovered")


async def device_responder(ws, calls: list[dict]):
    async for message in ws:
        if isinstance(message, bytes):
            continue
        decoded = json.loads(message)
        if decoded.get("type") != "mcp":
            continue
        request = decoded["payload"]
        if request["method"] == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-stackchan", "version": "test"},
            }
        elif request["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "self.get_device_status",
                        "description": "Get device status",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                    {
                        "name": "self.timer.start",
                        "description": "创建命名倒计时",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "duration_seconds": {"type": "integer", "minimum": 1},
                                "message": {"type": "string"},
                            },
                            "required": ["name", "duration_seconds"],
                        },
                    },
                ]
            }
        elif request["method"] == "tools/call":
            calls.append(request["params"])
            result = {
                "id": 7,
                "name": "厨房",
                "duration_seconds": 60,
                "remaining_seconds": 60,
                "status": "started",
            }
        else:
            result = {}
        await ws.send(
            json.dumps(
                {"type": "mcp", "payload": {"jsonrpc": "2.0", "id": request["id"], "result": result}},
                ensure_ascii=False,
            )
        )


async def exercise(bridge_port: int, calls: list[dict]):
    async with websockets.connect(f"ws://127.0.0.1:{bridge_port}/ws") as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 3,
                    "transport": "websocket",
                    "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
                }
            )
        )
        assert json.loads(await ws.recv())["type"] == "hello"
        responder = asyncio.create_task(device_responder(ws, calls))
        try:
            await wait_for_tools(bridge_port)
            response = await asyncio.to_thread(
                post_json,
                f"http://127.0.0.1:{bridge_port}/nanobot/message",
                {"text": "创建一个一分钟的厨房计时器"},
            )
            assert response["reply"] == "厨房计时器已开始。", response
        finally:
            responder.cancel()
            await asyncio.gather(responder, return_exceptions=True)


def main() -> int:
    bridge_port = free_port()
    mcp_port = free_port()
    llm_port = free_port()
    llm_state = FakeLLMState()
    llm_server = ThreadingHTTPServer(("127.0.0.1", llm_port), FakeLLMHandler)
    llm_server.state = llm_state
    llm_thread = threading.Thread(target=llm_server.serve_forever, daemon=True)
    llm_thread.start()

    with tempfile.TemporaryDirectory(prefix="stackchan-nanobot-loop-") as directory:
        temp = Path(directory)
        config = json.loads((ROOT / "nanobot_config/config.json").read_text(encoding="utf-8"))
        config["agents"]["defaults"]["workspace"] = str(temp / "workspace")
        config["tools"]["mcpServers"]["stackchan"]["url"] = f"http://127.0.0.1:{mcp_port}/mcp"
        config_path = temp / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "NANOBOT_WORKSPACE": str(temp / "workspace"),
                "OPENAI_API_KEY": "offline-test-key",
                "OPENAI_BASE_URL": f"http://127.0.0.1:{llm_port}/v1",
            }
        )
        process = subprocess.Popen(
            [
                str(PYTHON),
                "-u",
                str(ROOT / "nanobot_bridge/server.py"),
                "--host",
                "127.0.0.1",
                "--port",
                str(bridge_port),
                "--public-host",
                "127.0.0.1",
                "--mcp-port",
                str(mcp_port),
                "--nanobot-config",
                str(config_path),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        calls: list[dict] = []
        try:
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    get_json(f"http://127.0.0.1:{bridge_port}/health")
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                raise RuntimeError("bridge did not start")
            asyncio.run(exercise(bridge_port, calls))
        finally:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate(timeout=3)
            llm_server.shutdown()
            llm_server.server_close()
            llm_thread.join(3)

    assert len(llm_state.requests) == 2, llm_state.requests
    assert calls == [
        {
            "name": "self.timer.start",
            "arguments": {"name": "厨房", "duration_seconds": 60, "message": "厨房计时结束"},
        }
    ], calls
    tool_messages = [
        message for message in llm_state.requests[1]["messages"] if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1, tool_messages
    tool_result = json.loads(tool_messages[0]["content"])
    assert tool_result == {
        "id": 7,
        "name": "厨房",
        "duration_seconds": 60,
        "remaining_seconds": 60,
        "status": "started",
    }, tool_result
    assert "tools_used=['mcp_stackchan_self_timer_start']" in output, output
    print("stackchan Nanobot tool loop test ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
