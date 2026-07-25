from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "nanobot_bridge"))

from nanobot_runtime import NanobotRuntime  # noqa: E402


def test_explicit_empty_turn_registry_does_not_fall_back_to_global_tools():
    global_tools = SimpleNamespace(tool_names=[], get=lambda _name: None)
    bot = SimpleNamespace(_loop=SimpleNamespace(tools=global_tools))

    registry = NanobotRuntime._filtered_registry(bot, set())

    assert len(registry) == 0
    assert bool(registry) is True


def test_voice_registry_keeps_safe_web_tools_and_rejects_privileged_builtins(monkeypatch):
    tools = {
        name: SimpleNamespace(name=name)
        for name in ("web_search", "web_fetch", "exec", "write_file", "cron")
    }
    global_tools = SimpleNamespace(
        tool_names=list(tools),
        get=lambda name: tools.get(name),
    )
    bot = SimpleNamespace(_loop=SimpleNamespace(tools=global_tools))
    monkeypatch.delenv("STACKCHAN_SAFE_NANOBOT_TOOLS", raising=False)

    registry = NanobotRuntime._filtered_registry(bot, set())

    assert set(registry.tool_names) == {"web_search", "web_fetch"}


def test_voice_registry_only_exposes_selected_stackchan_mcp_tools():
    selected = SimpleNamespace(
        name="mcp_stackchan_self_robot_dance",
        _server_name="stackchan",
        _original_name="self.robot.dance",
    )
    rejected = SimpleNamespace(
        name="mcp_stackchan_self_reboot",
        _server_name="stackchan",
        _original_name="self.reboot",
    )
    tools = {selected.name: selected, rejected.name: rejected}
    bot = SimpleNamespace(
        _loop=SimpleNamespace(
            tools=SimpleNamespace(tool_names=list(tools), get=lambda name: tools.get(name))
        )
    )

    registry = NanobotRuntime._filtered_registry(bot, {"self.robot.dance"})

    assert registry.tool_names == ["mcp_stackchan_self_robot_dance"]
