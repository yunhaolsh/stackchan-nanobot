"""Persistent Nanobot runtime with per-turn StackChan tool selection."""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from capabilities import DeviceCapabilityGateway


_COMPACT_FALLBACK_PROMPT = """You are the StackChan voice agent running through Nanobot.
Reply in concise Chinese unless the user requests another language.
Use only Tools provided for the current turn, and invoke them only through structured Tool Calls.
Never print pseudo Tool syntax, XML Tool tags, or an unavailable Tool name.
After a successful device action, reply with one short confirmation sentence.
If a capability is unavailable, state the limitation instead of inventing an action.
For relative head movement, get current angles before setting new angles.
Timers and reminders support relative durations, not absolute clock schedules.
Do not use Markdown, lists, headings, or emoji in spoken replies."""
_DEFAULT_COMPACT_PROMPT_FILE = (
    Path(__file__).resolve().parents[1] / "nanobot_config/stackchan-voice-agent.md"
)


def _compact_system_prompt(
    context: Any,
    skill_names: list[str] | None = None,
    channel: str | None = None,
    session_summary: str | None = None,
    workspace: Any | None = None,
    include_memory_recent_history: bool = True,
    session_key: str | None = None,
    unified_session: bool = False,
) -> str:
    del channel, include_memory_recent_history, session_key, unified_session
    root = workspace or context.workspace
    workspace_policy = root / "STACKCHAN.md"
    configured_policy = Path(
        os.environ.get("STACKCHAN_COMPACT_PROMPT_FILE", _DEFAULT_COMPACT_PROMPT_FILE)
    ).expanduser()
    policy_path = workspace_policy if workspace_policy.is_file() else configured_policy
    policy = (
        policy_path.read_text(encoding="utf-8").strip()
        if policy_path.is_file()
        else _COMPACT_FALLBACK_PROMPT
    )
    parts = [policy]

    memory = context.memory.get_memory_context()
    if memory and not context._is_template_content(
        context.memory.read_memory(), "memory/MEMORY.md"
    ):
        parts.append(f"# Memory\n\n{memory}")

    configured_skills = {
        name.strip()
        for name in os.environ.get("STACKCHAN_COMPACT_SKILLS", "").split(",")
        if name.strip()
    }
    active_skills = sorted(configured_skills | set(skill_names or []))
    if active_skills:
        content = context.skills.load_skills_for_context(active_skills)
        if content:
            parts.append(f"# Active Skills\n\n{content}")
    if session_summary:
        parts.append(f"[Archived Context Summary]\n\n{session_summary}")
    return "\n\n---\n\n".join(parts)


@dataclass(slots=True)
class AgentResult:
    content: str
    tools_used: list[str]
    selected_tools: list[str] = field(default_factory=list)


class NanobotRuntime:
    _DEFAULT_SAFE_BUILTINS = frozenset({"web_search", "web_fetch"})

    def __init__(self, config_path: str | None, gateway: DeviceCapabilityGateway):
        self.config_path = config_path
        self.gateway = gateway
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="stackchan-nanobot", daemon=True)
        self._started = threading.Event()
        self._bot: Any = None
        self._bot_lock: asyncio.Lock | None = None
        self._loaded_generation = -1
        self._thread.start()
        if not self._started.wait(5):
            raise RuntimeError("Nanobot runtime event loop did not start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._bot_lock = asyncio.Lock()
        self._started.set()
        self._loop.run_forever()

    async def _ensure_bot(self):
        if not self.config_path:
            raise RuntimeError("NANOBOT_CONFIG is not configured")
        assert self._bot_lock is not None
        async with self._bot_lock:
            generation = self.gateway.generation
            if self._bot is not None and self._loaded_generation == generation:
                return self._bot
            if self._bot is not None:
                await self._bot.aclose()
            from nanobot import Nanobot
            from nanobot.providers.base import LLMProvider

            max_retries = max(0, int(os.environ.get("STACKCHAN_CHAT_MAX_RETRIES", "0")))
            LLMProvider._CHAT_RETRY_DELAYS = (1, 2, 4)[:max_retries]

            self._bot = Nanobot.from_config(config_path=self.config_path)
            if os.environ.get("STACKCHAN_COMPACT_PROMPT", "0") == "1":
                from types import MethodType

                self._bot._loop.context.build_system_prompt = MethodType(
                    _compact_system_prompt,
                    self._bot._loop.context,
                )
            await self._bot._loop._connect_mcp()
            self._loaded_generation = generation
            return self._bot

    @staticmethod
    def _filtered_registry(bot: Any, selected_raw_names: set[str]):
        from nanobot.agent.tools.registry import ToolRegistry

        class TurnToolRegistry(ToolRegistry):
            # Nanobot currently chooses `tools or self.tools`. An empty standard
            # registry is falsey and would expose every global tool. This registry
            # deliberately remains truthy so an explicit empty allowlist stays
            # empty for the turn.
            def __bool__(self) -> bool:
                return True

            def prepare_call(self, name: str, params: Any):
                prepared = super().prepare_call(name, params)
                if prepared[0] is None:
                    print(
                        f"[nanobot-tools] rejected_unavailable={name!r} "
                        f"allowed={sorted(self.tool_names)!r}"
                    )
                return prepared

        registry = TurnToolRegistry()
        configured_builtins = os.environ.get("STACKCHAN_SAFE_NANOBOT_TOOLS", "").strip()
        if configured_builtins.lower() in {"none", "off", "disabled"}:
            safe_builtins: set[str] = set()
        elif configured_builtins:
            safe_builtins = {
                name.strip() for name in configured_builtins.split(",") if name.strip()
            }
        else:
            safe_builtins = set(NanobotRuntime._DEFAULT_SAFE_BUILTINS)
        for wrapped_name in bot._loop.tools.tool_names:
            tool = bot._loop.tools.get(wrapped_name)
            if tool is None:
                continue
            raw_name = getattr(tool, "_original_name", None)
            server_name = getattr(tool, "_server_name", None)
            if server_name == "stackchan" and raw_name in selected_raw_names:
                registry.register(tool)
            elif server_name is None and wrapped_name in safe_builtins:
                registry.register(tool)
        return registry

    async def _run(self, prompt: str, session_key: str) -> AgentResult:
        from nanobot.agent.hook import SDKCaptureHook
        from nanobot.sdk.runtime import build_process_direct_kwargs
        from nanobot.sdk.types import result_from_response

        bot = await self._ensure_bot()
        selected = set(self.gateway.select_tools(prompt))
        tools = self._filtered_registry(bot, selected)
        print(
            f"[nanobot-tools] selected_raw={sorted(selected)!r} "
            f"selected_wrapped={sorted(tools.tool_names)!r}"
        )
        capture = SDKCaptureHook()
        kwargs = build_process_direct_kwargs(
            session_key=session_key,
            channel="stackchan",
            chat_id="device",
            sender_id="user",
            media=None,
            ephemeral=False,
        )
        response = await bot._loop.process_direct(
            prompt,
            **kwargs,
            hooks=[capture],
            tools=tools,
        )
        result = result_from_response(response, capture)
        return AgentResult(result.content or "", list(result.tools_used), sorted(selected))

    def run(self, prompt: str, session_key: str = "stackchan:device", timeout: float = 180.0) -> AgentResult:
        future: Future[AgentResult] = asyncio.run_coroutine_threadsafe(
            self._run(prompt, session_key), self._loop
        )
        return future.result(timeout=timeout)

    def invalidate_tools(self) -> None:
        self._loaded_generation = -1

    async def _close(self) -> None:
        if self._bot is not None:
            await self._bot.aclose()
            self._bot = None

    def stop(self, timeout: float = 5.0) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self._loop).result(timeout=timeout)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout)
