#!/usr/bin/env python3
"""Explain a StackChan image through an OpenAI-compatible vision endpoint."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import urllib.request
from pathlib import Path


def extract_content(payload: dict) -> str:
    content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        ).strip()
    return ""


def main() -> int:
    image_path = os.environ.get("STACKCHAN_VISION_IMAGE")
    if not image_path:
        print("STACKCHAN_VISION_IMAGE is not set", file=sys.stderr)
        return 2

    path = Path(image_path)
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    image_url = f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    payload = {
        "model": os.environ.get("STACKCHAN_VISION_MODEL", "Qwen3-VL-4B-Instruct"),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {
                        "type": "text",
                        "text": os.environ.get(
                            "STACKCHAN_VISION_QUESTION", "请简洁描述你看到的内容。"
                        ),
                    },
                ],
            }
        ],
        "stream": False,
        "max_tokens": int(os.environ.get("STACKCHAN_VISION_MAX_TOKENS", "512")),
    }
    base_url = (
        os.environ.get("STACKCHAN_VISION_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "http://127.0.0.1:18083/v1"
    ).rstrip("/")
    api_key = os.environ.get("STACKCHAN_VISION_API_KEY") or os.environ.get("OPENAI_API_KEY")
    headers = {"Content-Type": "application/json", "User-Agent": "stackchan-local-vision/1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(
        request, timeout=float(os.environ.get("STACKCHAN_VISION_TIMEOUT", "120"))
    ) as response:
        result = json.loads(response.read().decode("utf-8"))
    content = extract_content(result)
    if not content:
        raise RuntimeError("vision response did not contain text")
    print(content)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"stackchan vision failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
