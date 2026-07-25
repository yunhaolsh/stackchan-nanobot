#!/usr/bin/env python3
"""Bounded retry support for StackChan GLM media requests."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Callable


@dataclass(frozen=True)
class HttpResult:
    data: bytes
    headers: Message


def _retry_delay(exc: urllib.error.HTTPError, attempt: int, base_delay: float) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 15.0)
        except ValueError:
            pass
    return base_delay * (2**attempt)


def request_bytes(
    request: urllib.request.Request,
    *,
    timeout: int,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpResult:
    retries = max(int(os.environ.get("STACKCHAN_GLM_MAX_RETRIES", "2")), 0)
    base_delay = max(float(os.environ.get("STACKCHAN_GLM_RETRY_BACKOFF", "1")), 0.0)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResult(response.read(), response.headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {408, 409, 429} or 500 <= exc.code <= 599
            if retryable and attempt < retries:
                sleep(_retry_delay(exc, attempt, base_delay))
                continue
            raise RuntimeError(f"{label} HTTP {exc.code}: {detail[:1000]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                sleep(base_delay * (2**attempt))
                continue
            raise RuntimeError(f"{label} network error: {exc}") from exc
    raise RuntimeError(f"{label} request failed")
