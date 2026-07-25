"""Standard local Streamable HTTP MCP server backed by a StackChan device."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from capabilities import DeviceCapabilityGateway


class MCPHTTPService:
    def __init__(self, gateway: DeviceCapabilityGateway, host: str = "127.0.0.1", port: int = 12801):
        self.gateway = gateway
        self.host = host
        self.port = port
        self.server = Server(
            "stackchan-device",
            version="1.0.0",
            instructions="StackChan local capabilities guarded by the bridge permission policy.",
        )
        self._configure_handlers()
        self._manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=True,
            stateless=True,
        )
        self._uvicorn: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def _configure_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=tool.name,
                    description=tool.description,
                    inputSchema=tool.input_schema,
                )
                for tool in self.gateway.model_tools()
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            try:
                result = await asyncio.to_thread(self.gateway.call_tool, name, arguments)
                text = self.gateway.format_result(result)
                structured = result if isinstance(result, dict) else {"result": result}
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=text)],
                    structuredContent=structured,
                    isError=False,
                )
            except Exception as exc:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=str(exc))],
                    isError=True,
                )

    def _app(self) -> Starlette:
        async def mcp_asgi(scope, receive, send):
            await self._manager.handle_request(scope, receive, send)

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            async with self._manager.run():
                self._ready.set()
                yield

        return Starlette(routes=[Mount("/mcp", app=mcp_asgi)], lifespan=lifespan)

    def start(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        config = uvicorn.Config(
            self._app(),
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn.run, name="stackchan-mcp-http", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"MCP HTTP service did not start at http://{self.host}:{self.port}/mcp")

    def stop(self, timeout: float = 5.0) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)

