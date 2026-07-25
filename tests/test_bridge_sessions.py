from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import TimeoutError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "nanobot_bridge"))

import server  # noqa: E402
from capabilities import DeviceCapabilityGateway  # noqa: E402


def _state_with(*sessions: server.ClientSession):
    state = object.__new__(server.BridgeState)
    state.clients_lock = threading.RLock()
    state.ws_clients = {session.sock: session for session in sessions}
    return state


def test_current_client_moves_a_slow_turn_to_newest_device_connection():
    first_sock, first_peer = socket.socketpair()
    second_sock, second_peer = socket.socketpair()
    try:
        first = server.ClientSession(first_sock, device_key="device-1")
        second = server.ClientSession(second_sock, device_key="device-1")
        first.connected_at = 10
        second.connected_at = 20
        state = _state_with(first, second)

        assert state.current_client(first) is second
    finally:
        first_sock.close()
        first_peer.close()
        second_sock.close()
        second_peer.close()


def test_current_client_never_crosses_physical_devices():
    first_sock, first_peer = socket.socketpair()
    other_sock, other_peer = socket.socketpair()
    try:
        first = server.ClientSession(first_sock, device_key="device-1")
        other = server.ClientSession(other_sock, device_key="device-2")
        first.closed = True
        state = _state_with(first, other)

        assert state.current_client(first) is None
    finally:
        first_sock.close()
        first_peer.close()
        other_sock.close()
        other_peer.close()


def test_new_connection_replaces_same_device_without_old_detach_clearing_gateway():
    first_sock, first_peer = socket.socketpair()
    second_sock, second_peer = socket.socketpair()
    try:
        first = server.ClientSession(first_sock, device_key="device-1")
        second = server.ClientSession(second_sock, device_key="device-1")
        state = _state_with()
        state.gateway = DeviceCapabilityGateway()
        invalidations: list[bool] = []
        state.nanobot = SimpleNamespace(invalidate_tools=lambda: invalidations.append(True))

        state.register_client(first)
        state.register_client(second)

        assert first.closed is True
        assert second.closed is False
        assert state.current_client(second) is second
        assert state.gateway.connected is True
        assert list(state.ws_clients.values()) == [second]

        state.unregister_client(first)
        assert state.gateway.connected is True
        assert state.current_client(second) is second
    finally:
        first_sock.close()
        first_peer.close()
        second_sock.close()
        second_peer.close()


def test_mcp_discovery_retries_a_slow_first_initialize(monkeypatch):
    class SlowFirstSession:
        session_id = "session-1"
        device_key = "device-1"
        connected_at = 1
        closed = False
        mcp_ready = False

        def __init__(self):
            self.initialize_calls = 0
            self.sent: list[dict] = []

        def rpc(self, method: str, params: dict, timeout: float):
            if method == "initialize":
                self.initialize_calls += 1
                if self.initialize_calls == 1:
                    raise TimeoutError()
                return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
            assert method == "tools/list"
            return {"tools": [], "nextCursor": ""}

        def send_json(self, body: dict):
            self.sent.append(body)

    monkeypatch.setenv("STACKCHAN_MCP_INITIALIZE_ATTEMPTS", "2")
    session = SlowFirstSession()
    state = object.__new__(server.BridgeState)
    state.clients_lock = threading.RLock()
    state.ws_clients = {object(): session}
    state.gateway = DeviceCapabilityGateway()
    state.nanobot = SimpleNamespace(invalidate_tools=lambda: None)
    state.public_host = "127.0.0.1"
    state.port = 12800
    state.token = "test-token"

    state.discover_device(session)

    assert session.initialize_calls == 2
    assert session.mcp_ready is True
    assert state.gateway.connected is True
    assert any(message.get("text") == "设备响应较慢，正在自动重试" for message in session.sent)
    assert session.sent[-1].get("text") == "Nanobot 已就绪"
