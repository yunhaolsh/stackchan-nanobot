#!/usr/bin/env python3
"""Build a mode-specific Nanobot config without materializing API keys."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def build_runtime_config(
    source: dict[str, Any],
    *,
    mode: str,
    chat_model: str,
    chat_provider: str,
    thinking: str,
    local_model: str,
    local_context_tokens: int,
    local_max_tokens: int,
    local_max_messages: int = 20,
) -> dict[str, Any]:
    if mode not in {"cloud", "local", "hybrid"}:
        raise ValueError(f"unsupported inference mode: {mode}")

    config = deepcopy(source)
    presets = config.setdefault("modelPresets", {})
    primary = presets.setdefault("primary", {})
    primary["model"] = chat_model
    if mode == "local":
        primary["contextWindowTokens"] = local_context_tokens
        primary["maxTokens"] = local_max_tokens

    provider = config.setdefault("providers", {}).setdefault("openai", {})
    provider["apiKey"] = "${OPENAI_API_KEY}"
    provider["apiBase"] = "${OPENAI_BASE_URL}"
    provider["apiType"] = "chat_completions"
    if chat_provider in {"glm", "deepseek"}:
        provider["extraBody"] = {"thinking": {"type": thinking}}
    elif mode == "local":
        provider["extraBody"] = {
            "chat_template_kwargs": {"enable_thinking": False},
        }
    else:
        provider.pop("extraBody", None)

    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    if mode in {"local", "hybrid"}:
        defaults["maxMessages"] = max(1, local_max_messages)
    if mode == "hybrid":
        config["providers"]["stackchan_local"] = {
            "apiKey": "${STACKCHAN_LOCAL_CHAT_API_KEY}",
            "apiBase": "${STACKCHAN_LOCAL_CHAT_BASE_URL}",
            "extraBody": {
                "chat_template_kwargs": {"enable_thinking": False},
            },
        }
        presets["local_fallback"] = {
            "label": "StackChan local fallback",
            "provider": "stackchan_local",
            "model": local_model,
            "maxTokens": local_max_tokens,
            "contextWindowTokens": local_context_tokens,
            "temperature": primary.get("temperature", 0.2),
        }
        defaults["fallbackModels"] = ["local_fallback"]
    else:
        config["providers"].pop("stackchan_local", None)
        presets.pop("local_fallback", None)
        defaults.pop("fallbackModels", None)
        defaults.pop("fallback_models", None)
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--mode", required=True, choices=("cloud", "local", "hybrid"))
    parser.add_argument("--chat-model", required=True)
    parser.add_argument("--chat-provider", required=True)
    parser.add_argument("--thinking", default="disabled", choices=("disabled", "enabled"))
    parser.add_argument("--local-model", required=True)
    parser.add_argument("--local-context-tokens", type=int, default=16384)
    parser.add_argument("--local-max-tokens", type=int, default=1024)
    parser.add_argument("--local-max-messages", type=int, default=20)
    args = parser.parse_args()

    source = json.loads(args.source.read_text(encoding="utf-8"))
    config = build_runtime_config(
        source,
        mode=args.mode,
        chat_model=args.chat_model,
        chat_provider=args.chat_provider,
        thinking=args.thinking,
        local_model=args.local_model,
        local_context_tokens=args.local_context_tokens,
        local_max_tokens=args.local_max_tokens,
        local_max_messages=args.local_max_messages,
    )
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
