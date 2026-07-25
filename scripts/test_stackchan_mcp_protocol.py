#!/usr/bin/env python3
"""Offline end-to-end test for device MCP discovery, policy, and proxying."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-nanobot" / "bin" / "python"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


async def wait_for_device_tools(port: int, expected: int) -> dict:
    deadline = time.time() + 8
    while time.time() < deadline:
        health = await asyncio.to_thread(get_json, f"http://127.0.0.1:{port}/health")
        if health.get("device", {}).get("tool_count") == expected:
            return health
        await asyncio.sleep(0.1)
    raise AssertionError("device MCP tools were not discovered")


TOOLS_PAGE_1 = [
    {
        "name": "self.get_device_status",
        "description": "get status",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "self.robot.set_led_color",
        "description": "set LED color",
        "inputSchema": {
            "type": "object",
            "properties": {"rgb": {"type": "integer"}},
            "required": ["rgb"],
        },
    },
    {
        "name": "self.camera.take_photo",
        "description": "take a photo",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
]
TOOLS_PAGE_2 = [
    {
        "name": "self.timer.start",
        "description": "start timer",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "self.reboot",
        "description": "reboot device",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


async def device_responder(ws, calls: list[dict]):
    async for message in ws:
        if isinstance(message, bytes):
            continue
        decoded = json.loads(message)
        if decoded.get("type") != "mcp":
            continue
        request = decoded["payload"]
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-stackchan", "version": "test"},
            }
        elif method == "tools/list":
            if request.get("params", {}).get("cursor"):
                result = {"tools": TOOLS_PAGE_2}
            else:
                result = {"tools": TOOLS_PAGE_1, "nextCursor": "page-2"}
        elif method == "tools/call":
            calls.append(request["params"])
            result = {"success": True, "tool": request["params"]["name"]}
        else:
            result = {}
        await ws.send(
            json.dumps(
                {"type": "mcp", "payload": {"jsonrpc": "2.0", "id": request["id"], "result": result}}
            )
        )


async def exercise(bridge_port: int, mcp_port: int):
    calls: list[dict] = []
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
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello"
        responder = asyncio.create_task(device_responder(ws, calls))
        try:
            health = await wait_for_device_tools(bridge_port, 5)
            assert health["device"]["model_tool_count"] == 4

            async with streamable_http_client(f"http://127.0.0.1:{mcp_port}/mcp") as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    names = {item.name for item in listed.tools}
                    assert "self.robot.set_led_color" in names
                    assert "self.camera.take_photo" in names
                    assert "self.reboot" not in names

                    led = await client.call_tool("self.robot.set_led_color", {"rgb": 0x00FF00})
                    assert not led.isError
                    assert any(call["name"] == "self.robot.set_led_color" for call in calls)

                    camera = await client.call_tool("self.camera.take_photo", {"question": "看到了什么"})
                    assert not camera.isError
                    assert camera.structuredContent["status"] == "confirmation_required"
                    assert not any(call["name"] == "self.camera.take_photo" for call in calls)

                    reboot = await client.call_tool("self.reboot", {})
                    assert reboot.isError
                    assert not any(call["name"] == "self.reboot" for call in calls)

            pending = await asyncio.to_thread(
                get_json, f"http://127.0.0.1:{bridge_port}/permissions/pending"
            )
            action_id = pending["actions"][0]["id"]
            confirmed = await asyncio.to_thread(
                post_json,
                f"http://127.0.0.1:{bridge_port}/permissions/{action_id}/confirm",
            )
            assert confirmed["ok"] is True
            assert any(call["name"] == "self.camera.take_photo" for call in calls)
        finally:
            responder.cancel()
            await asyncio.gather(responder, return_exceptions=True)


def main() -> int:
    bridge_port = free_port()
    mcp_port = free_port()
    with tempfile.TemporaryDirectory(prefix="stackchan-mcp-test-") as workspace:
        env = os.environ.copy()
        env.update(
            {
                "NANOBOT_WORKSPACE": workspace,
                "OPENAI_API_KEY": "offline-test-key",
                "OPENAI_BASE_URL": "http://127.0.0.1:1",
            }
        )
        process = subprocess.Popen(
            [
                str(PYTHON),
                "-u",
                str(ROOT / "nanobot_bridge" / "server.py"),
                "--host",
                "127.0.0.1",
                "--port",
                str(bridge_port),
                "--public-host",
                "127.0.0.1",
                "--mcp-port",
                str(mcp_port),
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
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    get_json(f"http://127.0.0.1:{bridge_port}/health")
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                raise RuntimeError("bridge did not start")
            asyncio.run(exercise(bridge_port, mcp_port))
        finally:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate(timeout=3)
        assert "[mcp] discovered tools=5 model_visible=4 denied=1" in output, output
    print("stackchan MCP protocol test ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

