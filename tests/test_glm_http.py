from __future__ import annotations

import io
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import stackchan_glm_http  # noqa: E402


class _Response:
    def __init__(self, data: bytes):
        self._data = data
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._data


def _http_error(code: int) -> urllib.error.HTTPError:
    headers = Message()
    return urllib.error.HTTPError(
        "https://open.bigmodel.cn/test",
        code,
        "test",
        headers,
        io.BytesIO(b'{"error":"test"}'),
    )


def test_retries_transient_http_error(monkeypatch):
    calls = iter([_http_error(429), _Response(b'{"ok":true}')])

    def respond(*_args, **_kwargs):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setenv("STACKCHAN_GLM_MAX_RETRIES", "2")
    monkeypatch.setattr(urllib.request, "urlopen", respond)
    delays: list[float] = []

    result = stackchan_glm_http.request_bytes(
        urllib.request.Request("https://open.bigmodel.cn/test"),
        timeout=1,
        label="GLM Test",
        sleep=delays.append,
    )

    assert result.data == b'{"ok":true}'
    assert delays == [1.0]


def test_does_not_retry_authentication_error(monkeypatch):
    calls = 0

    def fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _http_error(401)

    monkeypatch.setenv("STACKCHAN_GLM_MAX_RETRIES", "2")
    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(RuntimeError, match="GLM Test HTTP 401"):
        stackchan_glm_http.request_bytes(
            urllib.request.Request("https://open.bigmodel.cn/test"),
            timeout=1,
            label="GLM Test",
            sleep=lambda _delay: None,
        )
    assert calls == 1
