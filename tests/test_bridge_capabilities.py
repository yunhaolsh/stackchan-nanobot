from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "nanobot_bridge"))

from capabilities import (  # noqa: E402
    DeviceCapabilityGateway,
    DeviceTool,
    DeviceUnavailable,
    PermissionDenied,
    PermissionTier,
    ToolPolicy,
    ToolRouter,
)


class FakeSession:
    session_id = "fake-device"

    def __init__(self):
        self.calls: list[tuple[str, dict, float]] = []

    def rpc(self, method: str, params: dict, timeout: float):
        self.calls.append((method, params, timeout))
        return {"ok": True, "name": params.get("name")}


def tool(name: str, description: str = "") -> DeviceTool:
    return DeviceTool(name, description or name, {"type": "object", "properties": {}})


def test_policy_is_fail_closed_for_dangerous_device_actions():
    policy = ToolPolicy()
    assert policy.classify("self.reboot") is PermissionTier.DENY
    assert policy.classify("self.upgrade_firmware") is PermissionTier.DENY
    assert policy.classify("self.network.configure") is PermissionTier.DENY
    assert policy.classify("self.screen.preview_image") is PermissionTier.DENY
    assert policy.classify("self.camera.take_photo") is PermissionTier.CONFIRM
    assert policy.classify("self.robot.set_led_color") is PermissionTier.AUTO


def test_camera_requires_confirmation_and_executes_once_after_confirmation():
    session = FakeSession()
    gateway = DeviceCapabilityGateway()
    gateway.attach(session, [tool("self.camera.take_photo"), tool("self.reboot")])

    pending = gateway.call_tool("self.camera.take_photo", {"question": "看到了什么"})
    assert pending["status"] == "confirmation_required"
    assert session.calls == []

    result = gateway.confirm(pending["confirmation_id"])
    assert result == {"ok": True, "name": "self.camera.take_photo"}
    assert len(session.calls) == 1
    assert session.calls[0][1]["arguments"] == {"question": "看到了什么"}
    assert session.calls[0][2] == 120.0

    with pytest.raises(PermissionDenied):
        gateway.call_tool("self.reboot")


def test_small_inventory_is_still_routed_to_the_relevant_capability_group():
    session = FakeSession()
    gateway = DeviceCapabilityGateway(router=ToolRouter(max_tools=20))
    inventory = [
        tool("self.robot.dance", "Run a dance motion"),
        tool("self.robot.set_head_angles", "Set head angles"),
        tool("self.timer.start", "Start timer"),
        tool("self.camera.take_photo", "Take photo"),
        tool("self.reboot", "Reboot device"),
    ]
    gateway.attach(session, inventory)

    selected = gateway.select_tools("跳个舞")

    assert selected == ["self.robot.dance"]


def test_gateway_exposes_no_tools_for_an_ordinary_chat_turn():
    session = FakeSession()
    gateway = DeviceCapabilityGateway()
    gateway.attach(
        session,
        [
            tool("self.robot.dance", "Run a dance motion"),
            tool("self.timer.start", "Start timer"),
        ],
    )

    assert gateway.select_tools("介绍一下你自己") == []


def test_camera_has_long_timeout_without_changing_ordinary_tool_timeout():
    session = FakeSession()
    gateway = DeviceCapabilityGateway(rpc_timeout=7, camera_rpc_timeout=90)
    gateway.attach(
        session,
        [tool("self.camera.take_photo"), tool("self.robot.set_led_color")],
    )

    gateway.call_tool("self.robot.set_led_color", {"red": 1})
    pending = gateway.call_tool("self.camera.take_photo", {"question": "测试"})
    gateway.confirm(pending["confirmation_id"])

    assert [call[2] for call in session.calls] == [7, 90]


def test_unconfigured_camera_backend_is_not_exposed_or_callable():
    session = FakeSession()
    gateway = DeviceCapabilityGateway(unavailable_markers=("camera.take_photo",))
    gateway.attach(
        session,
        [tool("self.camera.take_photo"), tool("self.robot.set_led_color")],
    )

    assert [item.name for item in gateway.all_tools()] == [
        "self.camera.take_photo",
        "self.robot.set_led_color",
    ]
    assert [item.name for item in gateway.model_tools()] == ["self.robot.set_led_color"]
    with pytest.raises(DeviceUnavailable, match="backend is not configured"):
        gateway.call_tool("self.camera.take_photo")


def test_router_caps_large_inventories_and_selects_relevant_tools():
    router = ToolRouter(max_tools=5)
    tools = [tool(f"self.misc.tool_{index}") for index in range(25)]
    tools.extend(
        [
            tool("self.timer.start", "创建命名倒计时"),
            tool("self.timer.cancel", "取消倒计时"),
            tool("self.robot.set_led_color", "设置灯光颜色"),
        ]
    )
    selected = router.select("请创建一个五分钟倒计时", tools)
    assert 1 <= len(selected) <= 5
    assert "self.timer.start" in selected
    assert "self.robot.set_led_color" not in selected


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍一下你自己",
        "帮我设置一个记忆项，八点吃晚饭",
        "提醒我晚上八点吃晚饭",
    ],
)
def test_router_exposes_no_device_tools_for_chat_or_unsupported_absolute_tasks(prompt):
    router = ToolRouter(max_tools=5)
    tools = [
        tool("self.get_device_status", "Get device status"),
        tool("self.timer.start", "Create a duration based countdown timer"),
        tool("self.robot.create_reminder", "Create a duration based reminder"),
    ]

    assert router.select(prompt, tools) == []


@pytest.mark.parametrize(
    "prompt",
    [
        "请向左旋转一下",
        "请向右转30度",
        "请向上转30度",
        "请低头15度",
    ],
)
def test_router_maps_chinese_rotation_to_head_angle_tools(prompt):
    router = ToolRouter(max_tools=5)
    tools = [
        tool("self.robot.get_head_angles", "Get head angles"),
        tool("self.robot.set_head_angles", "Set head yaw and pitch angles"),
        tool("self.timer.start", "创建倒计时"),
    ]

    selected = router.select(prompt, tools)

    assert "self.robot.set_head_angles" in selected
    assert "self.timer.start" not in selected


@pytest.mark.parametrize("prompt", ["来一段舞蹈", "请跳舞", "停止舞蹈"])
def test_router_maps_chinese_dance_requests_to_dance_tools(prompt):
    router = ToolRouter(max_tools=5)
    tools = [
        tool("self.robot.dance", "Run a dance motion"),
        tool("self.robot.stop_dance", "Stop the current dance motion"),
        tool("self.robot.set_head_angles", "Set head yaw and pitch angles"),
    ]

    selected = router.select(prompt, tools)

    assert "self.robot.dance" in selected
    assert "self.robot.stop_dance" in selected
    assert "self.robot.set_head_angles" not in selected
