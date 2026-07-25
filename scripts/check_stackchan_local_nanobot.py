#!/usr/bin/env python3
"""Verify local Qwen -> Nanobot -> MCP -> device-session Tool Call."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv-nanobot/bin/python"
try:
    import mcp  # noqa: F401
except ModuleNotFoundError:
    if VENV_PY.is_file() and Path(sys.prefix).resolve() != VENV_PY.parents[1].resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "nanobot_bridge"))

from build_nanobot_runtime_config import build_runtime_config  # noqa: E402
from capabilities import DeviceCapabilityGateway, DeviceTool  # noqa: E402
from mcp_http import MCPHTTPService  # noqa: E402
from nanobot_runtime import NanobotRuntime  # noqa: E402


class FakeStackChanSession:
    session_id = "local-check-device"

    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def rpc(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        self.calls.append((method, params, timeout))
        if method != "tools/call":
            raise RuntimeError(f"unexpected fake-device RPC: {method}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "status": "started",
                            "name": params["arguments"].get("name", "timer"),
                            "duration_seconds": params["arguments"]["duration_seconds"],
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "isError": False,
        }


def timer_tool() -> DeviceTool:
    return DeviceTool(
        name="self.timer.start",
        description=(
            "Create one named countdown timer. At most 8 timers can be active. "
            "Duration is in seconds."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                "message": {"type": "string"},
            },
            "required": ["name", "duration_seconds"],
        },
    )


def unrelated_tools() -> list[DeviceTool]:
    empty_schema = {"type": "object", "properties": {}}
    return [
        DeviceTool("self.get_device_status", "Get current device status", empty_schema),
        DeviceTool("self.robot.dance", "Run a dance motion", empty_schema),
        DeviceTool(
            "self.robot.set_led_color",
            "Set the device LED color",
            {
                "type": "object",
                "properties": {"color": {"type": "string"}},
                "required": ["color"],
            },
        ),
        DeviceTool("self.camera.take_photo", "Take one camera photo", empty_schema),
    ]


def main() -> int:
    mcp_port = int(os.environ.get("STACKCHAN_LOCAL_CHECK_MCP_PORT", "12811"))
    source = json.loads((ROOT / "nanobot_config/config.json").read_text(encoding="utf-8"))
    config = build_runtime_config(
        source,
        mode="local",
        chat_model=os.environ.get("STACKCHAN_LOCAL_CHAT_MODEL", "Qwen3-4B"),
        chat_provider="local",
        thinking="disabled",
        local_model=os.environ.get("STACKCHAN_LOCAL_CHAT_MODEL", "Qwen3-4B"),
        local_context_tokens=int(os.environ.get("STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS", "16384")),
        local_max_tokens=int(os.environ.get("STACKCHAN_LOCAL_CHAT_MAX_TOKENS", "1024")),
    )
    config["tools"]["mcpServers"]["stackchan"]["url"] = f"http://127.0.0.1:{mcp_port}/mcp"

    run_dir = ROOT / ".run"
    run_dir.mkdir(exist_ok=True)
    os.environ["OPENAI_API_KEY"] = os.environ.get(
        "STACKCHAN_LOCAL_CHAT_API_KEY", "local-no-secret"
    )
    os.environ["OPENAI_BASE_URL"] = os.environ.get(
        "STACKCHAN_LOCAL_CHAT_BASE_URL", "http://127.0.0.1:18080/v1"
    )
    os.environ["NANOBOT_WORKSPACE"] = str(run_dir / "local-check-workspace")
    os.environ["STACKCHAN_SAFE_NANOBOT_TOOLS"] = "none"
    os.environ["STACKCHAN_COMPACT_PROMPT"] = "1"

    session = FakeStackChanSession()
    gateway = DeviceCapabilityGateway(rpc_timeout=10)
    gateway.attach(session, [timer_tool(), *unrelated_tools()])
    mcp = MCPHTTPService(gateway, host="127.0.0.1", port=mcp_port)
    runtime: NanobotRuntime | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="nanobot-local-check-", dir=run_dir) as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            mcp.start()
            runtime = NanobotRuntime(str(config_path), gateway)
            started = time.monotonic()
            result = runtime.run(
                "请在设备上设置一个20秒的测试倒计时。必须调用设备工具。",
                session_key=f"stackchan-local-e2e:{time.time_ns()}",
                timeout=120,
            )
            elapsed_ms = round((time.monotonic() - started) * 1000)
    finally:
        if runtime is not None:
            runtime.stop()
        mcp.stop()

    tool_calls = [call for call in session.calls if call[0] == "tools/call"]
    if len(tool_calls) != 1:
        raise RuntimeError(f"expected one device Tool Call, got: {session.calls}")
    arguments = tool_calls[0][1].get("arguments") or {}
    if arguments.get("duration_seconds") != 20:
        raise RuntimeError(f"unexpected device Tool arguments: {arguments}")
    if not result.tools_used:
        raise RuntimeError(f"Nanobot did not report a used Tool: {result}")
    print(f"[check] nanobot reply={result.content!r}")
    print(f"[check] nanobot tools_used={result.tools_used!r}")
    print(f"[check] device arguments={arguments!r}")
    print(f"[check] nanobot MCP round trip elapsed_ms={elapsed_ms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
