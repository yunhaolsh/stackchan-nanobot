#!/usr/bin/env python3
"""Explain a StackChan camera image with a domestic Zhipu vision model."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from stackchan_glm_http import request_bytes


def _api_key() -> str:
    value = (
        os.environ.get("STACKCHAN_VISION_API_KEY")
        or os.environ.get("ZHIPU_API_KEY")
        or os.environ.get("GLM_API_KEY")
    )
    if not value:
        raise RuntimeError("STACKCHAN_VISION_API_KEY, ZHIPU_API_KEY, or GLM_API_KEY is not set")
    return value


def main() -> int:
    image_path = os.environ.get("STACKCHAN_VISION_IMAGE")
    if not image_path:
        print("STACKCHAN_VISION_IMAGE is not set", file=sys.stderr)
        return 2
    path = Path(image_path)
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    image_url = f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    question = os.environ.get("STACKCHAN_VISION_QUESTION", "请简洁描述你看到的内容。")
    payload = {
        "model": os.environ.get("STACKCHAN_VISION_MODEL", "glm-4.6v-flash"),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": question},
                ],
            }
        ],
        "stream": False,
        "max_tokens": int(os.environ.get("STACKCHAN_VISION_MAX_TOKENS", "512")),
    }
    base_url = os.environ.get("STACKCHAN_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "User-Agent": "stackchan-glm-vision/1",
        },
        method="POST",
    )
    response = request_bytes(
        request,
        timeout=int(os.environ.get("STACKCHAN_VISION_TIMEOUT", "90")),
        label="GLM Vision",
    )
    result = json.loads(response.data.decode("utf-8"))
    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if content:
        print(content.strip())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"stackchan GLM Vision failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
