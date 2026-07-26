from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_nanobot_runtime_config import build_runtime_config  # noqa: E402
from check_stackchan_local_inference import assert_tool_call, multipart_audio  # noqa: E402
from nanobot.config.schema import Config  # noqa: E402
from stackchan_provider_fallback import child_env, streaming_run, timeout_for  # noqa: E402
from stackchan_vision_openai import extract_content  # noqa: E402


def resolve_shell_mode(mode: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "ROOT_DIR": str(ROOT),
        "VENV_PY": str(ROOT / ".venv-nanobot/bin/python"),
        "STACKCHAN_INFERENCE_MODE": mode,
        **extra,
    }
    return subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail; source scripts/stackchan_inference_env.sh; "
            "printf '%s\\n' \"$CHAT_PROVIDER\" \"$STACKCHAN_CHAT_MODEL\" "
            "\"${STACKCHAN_ASR_PROVIDER:-}\" \"${STACKCHAN_ASR_COMMAND:-}\" "
            "\"${NANOBOT_OPENAI_COMPAT_TIMEOUT_S:-}\" "
            "\"${STACKCHAN_TTS_COMMAND:-}\" "
            "\"${STACKCHAN_COMPACT_PROMPT:-}\" "
            "\"${STACKCHAN_SESSION_NAMESPACE:-}\"",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_local_mode_does_not_require_cloud_credentials():
    completed = resolve_shell_mode("local")
    assert completed.returncode == 0, completed.stderr
    values = completed.stdout.splitlines()
    assert values[:3] == ["local", "Qwen3-4B", "local"]
    assert values[3].endswith("scripts/stackchan_asr_openai.py")
    assert values[4] == "60"
    assert values[6] == "1"
    assert values[7] == "stackchan:local"


def test_cloud_mode_still_requires_cloud_credentials():
    completed = resolve_shell_mode("cloud")
    assert completed.returncode != 0
    assert "Missing API key" in completed.stderr


def test_hybrid_mode_uses_nanobot_deadline_and_media_fallback():
    completed = resolve_shell_mode("hybrid", ZHIPU_API_KEY="offline-test-key")
    assert completed.returncode == 0, completed.stderr
    values = completed.stdout.splitlines()
    assert values[:3] == ["glm", "glm-4.7-flash", "glm"]
    assert "stackchan_provider_fallback.py --kind asr" in values[3]
    assert values[4] == "15"
    assert values[6] == "1"
    assert values[7] == "stackchan:hybrid"


def test_hybrid_nanobot_config_has_local_fallback_without_keys():
    source = json.loads((ROOT / "nanobot_config/config.json").read_text())
    config = build_runtime_config(
        source,
        mode="hybrid",
        chat_model="glm-4.7-flash",
        chat_provider="glm",
        thinking="disabled",
        local_model="Qwen3-4B",
        local_context_tokens=16384,
        local_max_tokens=1024,
    )
    assert config["agents"]["defaults"]["fallbackModels"] == ["local_fallback"]
    assert config["agents"]["defaults"]["maxMessages"] == 20
    assert config["modelPresets"]["local_fallback"]["provider"] == "stackchan_local"
    assert config["providers"]["stackchan_local"]["apiKey"] == "${STACKCHAN_LOCAL_CHAT_API_KEY}"
    Config.model_validate(config)
    serialized = json.dumps(config)
    assert "offline-test-key" not in serialized


def test_local_mode_reduces_context_and_disables_qwen_thinking():
    source = json.loads((ROOT / "nanobot_config/config.json").read_text())
    config = build_runtime_config(
        source,
        mode="local",
        chat_model="Qwen3-4B",
        chat_provider="local",
        thinking="disabled",
        local_model="Qwen3-4B",
        local_context_tokens=16384,
        local_max_tokens=512,
    )
    primary = config["modelPresets"]["primary"]
    assert primary["contextWindowTokens"] == 16384
    assert primary["maxTokens"] == 512
    assert config["providers"]["openai"]["extraBody"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert "fallbackModels" not in config["agents"]["defaults"]
    assert config["agents"]["defaults"]["maxMessages"] == 20
    Config.model_validate(config)


def test_local_mode_uses_local_timeout_instead_of_cloud_timeout():
    completed = resolve_shell_mode("local", STACKCHAN_CHAT_TIMEOUT="15")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines()[4] == "60"


@pytest.mark.parametrize("mode", ["cloud", "local", "hybrid"])
def test_generated_mode_config_validates_against_installed_nanobot(mode):
    source = json.loads((ROOT / "nanobot_config/config.json").read_text())
    chat_provider = "glm" if mode != "local" else "local"
    chat_model = "glm-4.7-flash" if mode != "local" else "Qwen3-4B"
    config = build_runtime_config(
        source,
        mode=mode,
        chat_model=chat_model,
        chat_provider=chat_provider,
        thinking="disabled",
        local_model="Qwen3-4B",
        local_context_tokens=16384,
        local_max_tokens=1024,
    )
    Config.model_validate(config)


def test_legacy_openai_media_flags_remain_supported():
    completed = resolve_shell_mode(
        "cloud",
        ZHIPU_API_KEY="offline-test-key",
        STACKCHAN_ASR_PROVIDER="custom",
        STACKCHAN_TTS_PROVIDER="custom",
        STACKCHAN_ENABLE_OPENAI_ASR="1",
        STACKCHAN_ENABLE_OPENAI_TTS="1",
    )
    assert completed.returncode == 0, completed.stderr
    values = completed.stdout.splitlines()
    assert values[3].endswith("scripts/stackchan_asr_openai.py")
    assert values[5].endswith("scripts/stackchan_tts_openai.py")


def test_command_line_mode_overrides_env_file(tmp_path):
    env_file = tmp_path / "stackchan.env"
    env_file.write_text("STACKCHAN_INFERENCE_MODE=cloud\n", encoding="utf-8")
    completed = subprocess.run(
        [str(ROOT / "scripts/start_stackchan_nanobot_hotspot.sh")],
        cwd=ROOT,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "STACKCHAN_CONFIG_ONLY": "1",
            "STACKCHAN_ENV_FILE": str(env_file),
            "STACKCHAN_INFERENCE_MODE": "local",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Inference mode   : local" in completed.stdout


def test_local_child_environment_overrides_cloud_component_values(monkeypatch):
    monkeypatch.setenv("STACKCHAN_ASR_MODEL", "cloud-model")
    monkeypatch.setenv("STACKCHAN_LOCAL_ASR_MODEL", "local-model")
    monkeypatch.setenv("STACKCHAN_LOCAL_ASR_BASE_URL", "http://127.0.0.1:18081/v1")
    env = child_env("local")
    assert env["STACKCHAN_ASR_MODEL"] == "local-model"
    assert env["STACKCHAN_ASR_BASE_URL"] == "http://127.0.0.1:18081/v1"


def test_tts_failure_after_output_is_marked_as_started(capsysbinary):
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'audio'); sys.stdout.flush(); raise SystemExit(1)",
    ]
    result = streaming_run(command, dict(os.environ), first_byte_timeout=2)
    assert result.ok is False
    assert result.output_started is True
    assert capsysbinary.readouterr().out == b"audio"


def test_tts_stall_after_output_does_not_allow_duplicate_fallback(capsysbinary):
    command = [
        sys.executable,
        "-c",
        "import sys,time; sys.stdout.buffer.write(b'audio'); sys.stdout.flush(); time.sleep(2)",
    ]
    result = streaming_run(command, dict(os.environ), first_byte_timeout=0.1)
    assert result.ok is False
    assert result.output_started is True
    assert "stalled" in result.reason
    assert capsysbinary.readouterr().out == b"audio"


def test_hybrid_tts_first_byte_timeout_uses_documented_setting(monkeypatch):
    monkeypatch.setenv("STACKCHAN_HYBRID_TTS_TIMEOUT_S", "12")
    monkeypatch.setenv("STACKCHAN_HYBRID_TTS_FIRST_BYTE_TIMEOUT_S", "3")

    assert timeout_for("cloud", "tts") == 3


def test_buffered_fallback_cli_uses_local_output():
    env = dict(os.environ)
    env.update(
        {
            "STACKCHAN_CLOUD_ASR_COMMAND": shlex.join([sys.executable, "-c", "raise SystemExit(1)"]),
            "STACKCHAN_LOCAL_ASR_COMMAND": shlex.join([sys.executable, "-c", "print('local transcript')"]),
            "STACKCHAN_HYBRID_ASR_TIMEOUT_S": "2",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/stackchan_provider_fallback.py"), "--kind", "asr"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "local transcript"
    assert "trying local" in completed.stderr


def test_vision_content_parser_supports_text_blocks():
    assert extract_content({"choices": [{"message": {"content": "desk"}}]}) == "desk"
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "a "},
                        {"type": "output_text", "text": "room"},
                    ]
                }
            }
        ]
    }
    assert extract_content(payload) == "a room"


def test_local_service_operator_scripts_are_executable():
    for name in (
        "check_stackchan_local_inference.py",
        "check_stackchan_local_nanobot.py",
        "setup_stackchan_local_speech.sh",
        "setup_stackchan_local_llm.sh",
        "start_stackchan_local_inference.sh",
        "stop_stackchan_local_inference.sh",
    ):
        assert os.access(ROOT / "scripts" / name, os.X_OK), name


def test_local_model_installers_pin_revisions_and_checksums():
    llm_setup = (ROOT / "scripts/setup_stackchan_local_llm.sh").read_text(encoding="utf-8")
    speech_setup = (ROOT / "scripts/setup_stackchan_local_speech.sh").read_text(
        encoding="utf-8"
    )

    assert "LLAMA_CPP_COMMIT" in llm_setup
    assert "MODEL_REVISION" in llm_setup
    assert "MODEL_SHA256" in llm_setup
    assert "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51" in speech_setup
    assert "bf30582eb1b012250a35b1a4a80e7dfbcf8485e7bb9de0d95efbbeef0e4ad86d" in speech_setup


def test_local_check_parses_structured_tool_call_and_builds_multipart():
    name, arguments = assert_tool_call(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "timer_start",
                                    "arguments": '{"duration_seconds":20}',
                                }
                            }
                        ]
                    }
                }
            ]
        }
    )
    assert name == "timer_start"
    assert arguments == {"duration_seconds": 20}
    body, content_type = multipart_audio(b"RIFF-audio", "SenseVoiceSmall")
    assert b"RIFF-audio" in body
    assert "multipart/form-data; boundary=" in content_type
