from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "nanobot_bridge"))

from capabilities import DeviceCapabilityGateway, DeviceTool  # noqa: E402
import server  # noqa: E402


class FakeSession:
    session_id = "fake-device"

    def __init__(self):
        self.calls: list[tuple[str, dict, float]] = []
        self.head_yaw = 0
        self.head_pitch = 0
        self.stopwatch_active = False
        self.stopwatch_running = False
        self.stopwatch_elapsed = 3
        self.focus_active = False
        self.todos: list[dict] = []

    def rpc(self, method: str, params: dict, timeout: float):
        self.calls.append((method, params, timeout))
        name = params.get("name")
        if name == "self.robot.get_head_angles":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f'{{"yaw": {self.head_yaw}, "pitch": {self.head_pitch}}}',
                    }
                ],
                "isError": False,
            }
        if name == "self.stopwatch.start":
            self.stopwatch_active = True
            self.stopwatch_running = True
            self.stopwatch_elapsed = 0
            return {"content": [{"type": "text", "text": '{"status":"started","elapsed_seconds":0}'}], "isError": False}
        if name == "self.stopwatch.status":
            active = "true" if self.stopwatch_active else "false"
            running = "true" if self.stopwatch_running else "false"
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f'{{"active":{active},"running":{running},'
                            f'"elapsed_seconds":{self.stopwatch_elapsed}}}'
                        ),
                    }
                ],
                "isError": False,
            }
        if name == "self.stopwatch.pause":
            if not self.stopwatch_active or not self.stopwatch_running:
                return {"content": [{"type": "text", "text": "false"}], "isError": False}
            self.stopwatch_running = False
            return {"content": [{"type": "text", "text": "true"}], "isError": False}
        if name == "self.stopwatch.resume":
            if not self.stopwatch_active or self.stopwatch_running:
                return {"content": [{"type": "text", "text": "false"}], "isError": False}
            self.stopwatch_running = True
            return {"content": [{"type": "text", "text": "true"}], "isError": False}
        if name == "self.stopwatch.stop":
            stopped = self.stopwatch_active
            self.stopwatch_active = False
            self.stopwatch_running = False
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f'{{"stopped":{"true" if stopped else "false"},'
                            f'"elapsed_seconds":{self.stopwatch_elapsed}}}'
                        ),
                    }
                ],
                "isError": False,
            }
        if name == "self.focus.start":
            self.focus_active = True
            duration = params.get("arguments", {}).get("duration_seconds", 1500)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f'{{"name":"专注","duration_seconds":{duration},'
                            f'"remaining_seconds":{duration},"status":"started"}}'
                        ),
                    }
                ],
                "isError": False,
            }
        if name == "self.focus.status":
            active = "true" if self.focus_active else "false"
            return {"content": [{"type": "text", "text": f'{{"active":{active},"remaining_seconds":300}}'}]}
        if name == "self.focus.stop":
            stopped = self.focus_active
            self.focus_active = False
            return {"content": [{"type": "text", "text": "true" if stopped else "false"}], "isError": False}
        if name == "self.todo.add":
            text = params.get("arguments", {}).get("text", "")
            self.todos.append({"id": len(self.todos) + 1, "text": text, "done": False})
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f'{{"id":{len(self.todos)},"text":"{text}","done":false}}',
                    }
                ],
                "isError": False,
            }
        if name == "self.todo.list":
            import json

            return {
                "content": [{"type": "text", "text": json.dumps(self.todos, ensure_ascii=False)}],
                "isError": False,
            }
        if name == "self.todo.delete":
            text = params.get("arguments", {}).get("text", "")
            before = len(self.todos)
            self.todos = [todo for todo in self.todos if todo["text"] != text]
            return {"content": [{"type": "text", "text": "true" if len(self.todos) < before else "false"}]}
        if name == "self.todo.clear":
            deleted = len([todo for todo in self.todos if not todo["done"]])
            self.todos = [todo for todo in self.todos if todo["done"]]
            return {"content": [{"type": "text", "text": f'{{"deleted":{deleted}}}'}]}
        if name == "self.todo.complete":
            text = params.get("arguments", {}).get("text", "")
            for todo in self.todos:
                if todo["text"] == text:
                    todo["done"] = True
                    return {"content": [{"type": "text", "text": "true"}]}
            return {"content": [{"type": "text", "text": "false"}]}
        return {"ok": True, "name": name}


def tool(name: str) -> DeviceTool:
    return DeviceTool(name, name, {"type": "object", "properties": {}})


def make_state(*tool_names: str):
    fake = FakeSession()
    gateway = DeviceCapabilityGateway(rpc_timeout=3)
    gateway.attach(fake, [tool(name) for name in tool_names])
    return SimpleNamespace(gateway=gateway), fake


def test_fast_action_executes_head_turn_without_llm():
    state, fake = make_state("self.robot.get_head_angles", "self.robot.set_head_angles")

    reply = server._try_fast_local_action(state, "请向右转头部180度。")

    assert reply == "已向右转180度，当前水平角度128度。"
    assert fake.calls == [
        (
            "tools/call",
            {
                "name": "self.robot.get_head_angles",
                "arguments": {},
            },
            3,
        ),
        (
            "tools/call",
            {
                "name": "self.robot.set_head_angles",
                "arguments": {"yaw": 128, "pitch": 0, "speed": 500},
            },
            3,
        )
    ]


def test_fast_action_executes_relative_head_turn_without_llm():
    state, fake = make_state("self.robot.get_head_angles", "self.robot.set_head_angles")
    fake.head_yaw = 10
    fake.head_pitch = 40

    reply = server._try_fast_local_action(state, "向左转头30度")

    assert reply == "已向左转30度，当前水平角度-20度。"
    assert fake.calls[-1][1] == {
        "name": "self.robot.set_head_angles",
        "arguments": {"yaw": -20, "pitch": 40, "speed": 500},
    }


def test_fast_action_reports_command_direction_when_target_yaw_stays_positive():
    state, fake = make_state("self.robot.get_head_angles", "self.robot.set_head_angles")
    fake.head_yaw = 33
    fake.head_pitch = 40

    reply = server._try_fast_local_action(state, "请向左转10度。")

    assert reply == "已向左转10度，当前水平角度23度。"
    assert fake.calls[-1][1] == {
        "name": "self.robot.set_head_angles",
        "arguments": {"yaw": 23, "pitch": 40, "speed": 500},
    }


def test_fast_action_executes_countdown_without_llm():
    state, fake = make_state("self.timer.start")

    reply = server._try_fast_local_action(state, "设置一个二十秒倒计时")

    assert reply == "已启动20秒倒计时。"
    assert fake.calls[0][1] == {
        "name": "self.timer.start",
        "arguments": {"name": "20秒倒计时", "duration_seconds": 20},
    }


def test_fast_action_executes_focus_mode_without_llm():
    state, fake = make_state("self.focus.start")

    reply = server._try_fast_local_action(state, "开始25分钟专注模式")

    assert reply == "已启动25分钟专注模式。"
    assert fake.calls[-1][1] == {
        "name": "self.focus.start",
        "arguments": {
            "duration_seconds": 1500,
            "message": "专注时间结束，休息一下吧。",
        },
    }


def test_fast_action_executes_stopwatch_without_llm():
    state, fake = make_state("self.stopwatch.start", "self.stopwatch.status", "self.stopwatch.stop")

    assert server._try_fast_local_action(state, "开始秒表") == "秒表已开始。"
    assert "秒表已运行" in server._try_fast_local_action(state, "查询秒表")
    assert "秒表已停止" in server._try_fast_local_action(state, "停止秒表")
    assert [call[1]["name"] for call in fake.calls] == [
        "self.stopwatch.start",
        "self.stopwatch.status",
        "self.stopwatch.stop",
    ]


def test_fast_action_executes_dance_with_visible_style():
    state, fake = make_state("self.robot.dance")

    reply = server._try_fast_local_action(state, "跳个舞")

    assert reply == "开始跳舞。"
    assert fake.calls == [
        (
            "tools/call",
            {
                "name": "self.robot.dance",
                "arguments": {"style": "robot"},
            },
            3,
        )
    ]


def test_fast_action_executes_todo_add_list_delete_without_llm():
    state, fake = make_state(
        "self.todo.add",
        "self.todo.list",
        "self.todo.delete",
        "self.todo.clear",
        "self.todo.complete",
    )

    assert server._try_fast_local_action(state, "添加待办晚上八点吃饭") == "已添加待办。"
    assert fake.todos == [{"id": 1, "text": "晚上八点吃饭", "done": False}]
    assert server._try_fast_local_action(state, "后面有什么安排") == "后面的安排有：晚上八点吃饭。"
    assert server._try_fast_local_action(state, "删除待办晚上八点吃饭") == "已删除这条待办。"
    assert fake.todos == []


def test_fast_action_executes_todo_clear_without_llm():
    state, fake = make_state("self.todo.add", "self.todo.clear")

    assert server._try_fast_local_action(state, "添加待办买牛奶") == "已添加待办。"
    assert server._try_fast_local_action(state, "删除所有待办事项") == "已删除1条待办。"
    assert fake.todos == []
