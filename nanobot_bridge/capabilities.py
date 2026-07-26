"""Thread-safe StackChan capability inventory, routing, and permission policy."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class DeviceRPCSession(Protocol):
    session_id: str

    def rpc(self, method: str, params: dict[str, Any], timeout: float) -> Any: ...


class DeviceUnavailable(RuntimeError):
    pass


class PermissionDenied(RuntimeError):
    pass


class PermissionTier(str, Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class DeviceTool:
    name: str
    description: str
    input_schema: dict[str, Any]

    @classmethod
    def from_mcp(cls, payload: dict[str, Any]) -> "DeviceTool":
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("device tool is missing name")
        schema = payload.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        return cls(
            name=name,
            description=str(payload.get("description") or name),
            input_schema=schema,
        )


@dataclass(slots=True)
class PendingAction:
    id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    created_at: float = field(default_factory=time.time)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool_name,
            "arguments": self.arguments,
            "created_at": self.created_at,
        }


class ToolPolicy:
    """Fail-closed policy for model-originated device actions."""

    _DENIED_MARKERS = (
        "reboot",
        "upgrade",
        "firmware",
        "network",
        "wifi",
        "factory_reset",
        "assets.set_download_url",
        "screen.snapshot",
        "screen.preview_image",
    )
    _CONFIRM_MARKERS = ("camera.take_photo",)

    def classify(self, tool_name: str) -> PermissionTier:
        lowered = tool_name.lower()
        if any(marker in lowered for marker in self._DENIED_MARKERS):
            return PermissionTier.DENY
        if any(marker in lowered for marker in self._CONFIRM_MARKERS):
            return PermissionTier.CONFIRM
        return PermissionTier.AUTO


class ToolRouter:
    """Select a bounded, prompt-relevant subset of the current device tools."""

    _GROUP_KEYWORDS = {
        "timer": ("timer", "reminder", "计时", "倒计时", "定时", "提醒"),
        "head": (
            "head",
            "angle",
            "turn",
            "rotate",
            "转头",
            "旋转",
            "抬头",
            "低头",
            "仰头",
            "俯视",
            "向左",
            "向右",
            "向上",
            "向下",
            "左转",
            "右转",
            "方向",
            "角度",
        ),
        "led": ("led", "light", "color", "灯", "颜色", "亮灯"),
        "audio": ("audio", "speaker", "volume", "声音", "音量", "静音"),
        "screen": ("screen", "brightness", "theme", "屏幕", "亮度", "主题"),
        "camera": (
            "camera",
            "photo",
            "vision",
            "拍照",
            "相机",
            "照片",
            "画面",
            "镜头",
            "看到了什么",
            "看看周围",
        ),
        "dance": (
            "dance",
            "motion",
            "跳舞",
            "跳个舞",
            "舞蹈",
            "动作",
            "停止跳舞",
            "停止舞蹈",
        ),
        "status": ("status", "battery", "network", "状态", "电量", "联网"),
    }

    def __init__(self, max_tools: int = 8):
        self.max_tools = max(1, max_tools)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{1,6}", text.lower()))

    @staticmethod
    def _narrow_tools(
        prompt: str,
        active_groups: set[str],
        tools: list[DeviceTool],
    ) -> list[DeviceTool]:
        """Keep only the operation schemas needed for an unambiguous turn."""
        if len(active_groups) != 1:
            return tools
        group = next(iter(active_groups))
        lowered = prompt.lower()

        if group == "timer":
            if any(word in lowered for word in ("倒计时", "计时器", "timer")):
                tools = [tool for tool in tools if ".timer." in tool.name.lower()]
            elif any(word in lowered for word in ("提醒", "reminder")):
                tools = [tool for tool in tools if "reminder" in tool.name.lower()]

            operation_markers: tuple[str, ...] = ()
            if any(word in lowered for word in ("取消", "删除", "停止", "cancel")):
                operation_markers = (".cancel", "stop_reminder")
            elif any(word in lowered for word in ("暂停", "pause")):
                operation_markers = (".pause",)
            elif any(word in lowered for word in ("继续", "恢复", "resume")):
                operation_markers = (".resume",)
            elif any(word in lowered for word in ("查看", "查询", "列表", "还有", "剩余", "多久", "list")):
                operation_markers = (".list", "get_reminders")
            elif any(word in lowered for word in ("设置", "创建", "开始", "启动", "提醒我", "start", "create")):
                operation_markers = (".start", "create_reminder")
            if operation_markers:
                matched = [
                    tool
                    for tool in tools
                    if any(marker in tool.name.lower() for marker in operation_markers)
                ]
                if matched:
                    return matched

        if group == "head":
            if any(word in lowered for word in ("多少", "当前", "获取", "查询", "位置")) and not any(
                word in lowered for word in ("转", "旋转", "抬", "低头", "仰", "俯", "设置")
            ):
                matched = [tool for tool in tools if "get_head_angles" in tool.name.lower()]
            else:
                matched = [
                    tool
                    for tool in tools
                    if any(marker in tool.name.lower() for marker in ("get_head_angles", "set_head_angles"))
                ]
            if matched:
                return matched

        if group == "dance":
            marker = "stop_dance" if any(word in lowered for word in ("停止", "别跳", "stop")) else ".dance"
            matched = [tool for tool in tools if marker in tool.name.lower()]
            if matched:
                return matched

        if group == "camera":
            matched = [tool for tool in tools if "camera.take_photo" in tool.name.lower()]
            if matched:
                return matched

        if group == "led":
            matched = [tool for tool in tools if "set_led_color" in tool.name.lower()]
            if matched:
                return matched

        if group == "audio":
            marker = "get_device_status" if any(word in lowered for word in ("多少", "当前", "查询")) else "set_volume"
            matched = [tool for tool in tools if marker in tool.name.lower()]
            if matched:
                return matched

        if group == "screen":
            markers = ("screen.get_info",) if any(word in lowered for word in ("多少", "当前", "查询")) else (
                "screen.set_brightness",
                "screen.set_theme",
            )
            matched = [tool for tool in tools if any(marker in tool.name.lower() for marker in markers)]
            if matched:
                return matched

        if group == "status":
            matched = [
                tool
                for tool in tools
                if any(marker in tool.name.lower() for marker in ("get_device_status", "get_system_info"))
            ]
            if matched:
                return matched
        return tools

    def select(self, prompt: str, tools: list[DeviceTool]) -> list[str]:
        if not tools:
            return []
        prompt_lower = prompt.lower()
        prompt_tokens = self._tokens(prompt)
        active_groups = {
            group
            for group, words in self._GROUP_KEYWORDS.items()
            if any(word in prompt_lower for word in words)
        }
        # A turn without a device intent must not inherit a generic device
        # capability. This keeps normal conversation and unsupported product
        # concepts (for example a "memory item") out of the MCP execution path.
        if not active_groups:
            return []

        # The current firmware timers are duration based. An absolute wall-clock
        # request needs a calendar/todo capability that is not implemented yet;
        # exposing countdown tools here would invite the model to guess a delay.
        if active_groups == {"timer"} and re.search(
            r"(?:今天|明天|后天|早上|上午|中午|下午|晚上|凌晨)?\s*[零〇一二两三四五六七八九十百\d]{1,4}\s*点",
            prompt,
        ):
            return []

        tools = self._narrow_tools(prompt, active_groups, tools)

        scored: list[tuple[int, str]] = []
        for tool in tools:
            haystack = f"{tool.name} {tool.description}".lower()
            score = len(prompt_tokens & self._tokens(haystack)) * 2
            for group in active_groups:
                if any(word in haystack for word in self._GROUP_KEYWORDS[group]):
                    score += 20
            scored.append((score, tool.name))

        scored.sort(key=lambda item: (-item[0], item[1]))
        positive = [name for score, name in scored if score > 0]
        return positive[: self.max_tools]


class DeviceCapabilityGateway:
    def __init__(
        self,
        *,
        policy: ToolPolicy | None = None,
        router: ToolRouter | None = None,
        rpc_timeout: float = 20.0,
        camera_rpc_timeout: float = 120.0,
        confirmation_ttl: float = 120.0,
        unavailable_markers: tuple[str, ...] = (),
    ):
        self.policy = policy or ToolPolicy()
        self.router = router or ToolRouter()
        self.rpc_timeout = rpc_timeout
        self.camera_rpc_timeout = max(rpc_timeout, camera_rpc_timeout)
        self.confirmation_ttl = confirmation_ttl
        self.unavailable_markers = tuple(marker.lower() for marker in unavailable_markers)
        self._lock = threading.RLock()
        self._session: DeviceRPCSession | None = None
        self._tools: dict[str, DeviceTool] = {}
        self._pending: dict[str, PendingAction] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._session is not None

    def attach(self, session: DeviceRPCSession, tools: list[DeviceTool]) -> bool:
        inventory = {tool.name: tool for tool in tools}
        with self._lock:
            changed = inventory != self._tools or session is not self._session
            self._session = session
            self._tools = inventory
            if changed:
                self._generation += 1
            return changed

    def detach(self, session: DeviceRPCSession) -> bool:
        with self._lock:
            if self._session is not session:
                return False
            self._session = None
            self._tools = {}
            self._pending = {
                action_id: action
                for action_id, action in self._pending.items()
                if action.session_id != session.session_id
            }
            self._generation += 1
            return True

    def all_tools(self) -> list[DeviceTool]:
        with self._lock:
            return sorted(self._tools.values(), key=lambda tool: tool.name)

    def model_tools(self) -> list[DeviceTool]:
        return [
            tool
            for tool in self.all_tools()
            if self.policy.classify(tool.name) is not PermissionTier.DENY
            and self._is_available(tool.name)
        ]

    def _is_available(self, tool_name: str) -> bool:
        lowered = tool_name.lower()
        return not any(marker in lowered for marker in self.unavailable_markers)

    def select_tools(self, prompt: str) -> list[str]:
        return self.router.select(prompt, self.model_tools())

    def _current(self, tool_name: str) -> tuple[DeviceRPCSession, DeviceTool]:
        with self._lock:
            session = self._session
            tool = self._tools.get(tool_name)
        if session is None:
            raise DeviceUnavailable("StackChan is not connected")
        if tool is None:
            raise DeviceUnavailable(f"StackChan tool is unavailable: {tool_name}")
        return session, tool

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        arguments = dict(arguments or {})
        if not self._is_available(tool_name):
            raise DeviceUnavailable(f"tool backend is not configured: {tool_name}")
        session, _ = self._current(tool_name)
        tier = self.policy.classify(tool_name)
        if tier is PermissionTier.DENY:
            raise PermissionDenied(f"tool is forbidden by local policy: {tool_name}")
        if tier is PermissionTier.CONFIRM:
            action = PendingAction(
                id=uuid.uuid4().hex,
                session_id=session.session_id,
                tool_name=tool_name,
                arguments=arguments,
            )
            with self._lock:
                self._expire_pending_locked()
                self._pending[action.id] = action
            return {
                "status": "confirmation_required",
                "confirmation_id": action.id,
                "message": "需要用户明确确认后才能使用摄像头。请询问用户，并等待用户说“确认”。",
            }
        return self._execute(session, tool_name, arguments)

    def _execute(
        self, session: DeviceRPCSession, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        timeout = (
            self.camera_rpc_timeout
            if "camera.take_photo" in tool_name.lower()
            else self.rpc_timeout
        )
        response = session.rpc(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout,
        )
        return response

    def pending_actions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_pending_locked()
            return [action.public_dict() for action in self._pending.values()]

    def latest_pending_id(self, session_id: str | None = None) -> str | None:
        with self._lock:
            self._expire_pending_locked()
            candidates = [
                action
                for action in self._pending.values()
                if session_id is None or action.session_id == session_id
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda action: action.created_at).id

    def confirm(self, action_id: str) -> Any:
        with self._lock:
            self._expire_pending_locked()
            action = self._pending.pop(action_id, None)
            session = self._session
        if action is None:
            raise PermissionDenied("confirmation is missing or expired")
        if session is None or session.session_id != action.session_id:
            raise DeviceUnavailable("the device session for this confirmation is no longer connected")
        return self._execute(session, action.tool_name, action.arguments)

    def cancel(self, action_id: str) -> bool:
        with self._lock:
            return self._pending.pop(action_id, None) is not None

    def _expire_pending_locked(self) -> None:
        deadline = time.time() - self.confirmation_ttl
        self._pending = {
            action_id: action
            for action_id, action in self._pending.items()
            if action.created_at >= deadline
        }

    def health(self) -> dict[str, Any]:
        tools = self.all_tools()
        return {
            "connected": self.connected,
            "tool_count": len(tools),
            "model_tool_count": sum(
                self.policy.classify(tool.name) is not PermissionTier.DENY for tool in tools
            ),
            "pending_confirmations": len(self.pending_actions()),
            "generation": self.generation,
        }

    @staticmethod
    def format_result(result: Any) -> str:
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
