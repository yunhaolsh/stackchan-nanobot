#!/usr/bin/env python3
"""Run a cloud media adapter and fall back locally before output starts."""

from __future__ import annotations

import argparse
import os
import selectors
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass(slots=True)
class RunResult:
    ok: bool
    stdout: bytes = b""
    stderr: bytes = b""
    reason: str = ""
    output_started: bool = False


def child_env(tier: str) -> dict[str, str]:
    env = dict(os.environ)
    prefix = f"STACKCHAN_{tier.upper()}_"
    for key, value in os.environ.items():
        if key.startswith(prefix):
            env[f"STACKCHAN_{key[len(prefix):]}"] = value
    return env


def command_for(tier: str, kind: str) -> list[str]:
    value = os.environ.get(f"STACKCHAN_{tier.upper()}_{kind.upper()}_COMMAND", "").strip()
    if not value:
        raise RuntimeError(f"missing {tier} {kind} command")
    return shlex.split(value)


def buffered_run(command: list[str], env: dict[str, str], timeout: float) -> RunResult:
    try:
        completed = subprocess.run(command, env=env, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return RunResult(False, stderr=exc.stderr or b"", reason=f"timeout after {timeout:g}s")
    output = completed.stdout or b""
    ok = completed.returncode == 0 and bool(output.strip())
    return RunResult(ok, output, completed.stderr or b"", f"exit={completed.returncode}", bool(output))


def terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def streaming_run(command: list[str], env: dict[str, str], first_byte_timeout: float) -> RunResult:
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + first_byte_timeout
    emitted = False
    stderr = bytearray()

    try:
        while selector.get_map():
            timeout = max(0.0, deadline - time.monotonic())
            events = selector.select(timeout)
            if not events:
                terminate(process)
                reason = (
                    f"output stalled for {first_byte_timeout:g}s"
                    if emitted
                    else f"no output after {first_byte_timeout:g}s"
                )
                return RunResult(
                    False,
                    stderr=bytes(stderr),
                    reason=reason,
                    output_started=emitted,
                )
            for key, _ in events:
                chunk = key.fileobj.read1(65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr.extend(chunk)
                else:
                    emitted = True
                    deadline = time.monotonic() + first_byte_timeout
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
        return_code = process.wait()
    finally:
        selector.close()
        if process.poll() is None:
            terminate(process)

    if emitted:
        if stderr:
            sys.stderr.buffer.write(stderr)
        return RunResult(
            return_code == 0,
            stderr=bytes(stderr),
            reason=f"exit={return_code}",
            output_started=True,
        )
    return RunResult(False, stderr=bytes(stderr), reason=f"exit={return_code}")


def timeout_for(tier: str, kind: str) -> float:
    if tier == "cloud":
        defaults = {"asr": "8", "tts": "8", "vision": "30"}
        names = [f"STACKCHAN_HYBRID_{kind.upper()}_TIMEOUT_S"]
        if kind == "tts":
            names.insert(0, "STACKCHAN_HYBRID_TTS_FIRST_BYTE_TIMEOUT_S")
    else:
        defaults = {"asr": "60", "tts": "60", "vision": "120"}
        names = [f"STACKCHAN_LOCAL_{kind.upper()}_TIMEOUT_S"]
        if kind == "tts":
            names.insert(0, "STACKCHAN_LOCAL_TTS_FIRST_BYTE_TIMEOUT_S")
    value = next((os.environ[name] for name in names if os.environ.get(name)), defaults[kind])
    return float(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=("asr", "tts", "vision"))
    args = parser.parse_args()

    if args.kind == "tts":
        primary = streaming_run(command_for("cloud", args.kind), child_env("cloud"), timeout_for("cloud", args.kind))
        if primary.ok or primary.output_started:
            return 0 if primary.ok else 1
        print(f"[provider-fallback] cloud tts failed: {primary.reason}; trying local", file=sys.stderr)
        fallback = streaming_run(command_for("local", args.kind), child_env("local"), timeout_for("local", args.kind))
        return 0 if fallback.ok else 1

    primary = buffered_run(command_for("cloud", args.kind), child_env("cloud"), timeout_for("cloud", args.kind))
    if primary.ok:
        sys.stdout.buffer.write(primary.stdout)
        if primary.stderr:
            sys.stderr.buffer.write(primary.stderr)
        return 0
    print(f"[provider-fallback] cloud {args.kind} failed: {primary.reason}; trying local", file=sys.stderr)
    fallback = buffered_run(command_for("local", args.kind), child_env("local"), timeout_for("local", args.kind))
    if fallback.stderr:
        sys.stderr.buffer.write(fallback.stderr)
    if fallback.ok:
        sys.stdout.buffer.write(fallback.stdout)
        return 0
    print(f"[provider-fallback] local {args.kind} failed: {fallback.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
