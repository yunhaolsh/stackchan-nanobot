#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _print(status: str, name: str, detail: str = ""):
    print(f"{status:<5} {name}{': ' + detail if detail else ''}")


def _http_json(url: str, timeout: float = 3.0) -> tuple[int, dict | None, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "stackchan-diagnose/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw), raw
            except json.JSONDecodeError:
                return resp.status, None, raw
    except urllib.error.HTTPError as exc:
        return exc.code, None, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, None, str(exc)


def _post_json(url: str, body: dict, timeout: float = 5.0) -> tuple[int, dict | None, str]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "stackchan-diagnose/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw), raw
            except json.JSONDecodeError:
                return resp.status, None, raw
    except urllib.error.HTTPError as exc:
        return exc.code, None, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, None, str(exc)


def _tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    data = path.read_bytes()[-256_000:]
    text = data.replace(b"\x00", b"").decode("utf-8", errors="replace")
    return text.splitlines()[-limit:]


def _count_markers(lines: list[str]) -> dict[str, int]:
    markers = {
        "ota": "[ota]",
        "ws_connected": "[ws] connected",
        "audio": "[ws] audio frame",
        "listen_start": "[listen] start",
        "listen_stop": "[listen] stop",
        "asr": "[asr]",
        "tts": "[tts]",
    }
    return {name: sum(1 for line in lines if marker in line) for name, marker in markers.items()}


def _last_matching(lines: list[str], token: str) -> str:
    for line in reversed(lines):
        if token in line:
            return line
    return ""


def _ss_connections(port: int) -> str:
    try:
        result = subprocess.run(
            ["ss", "-tnp", f"sport = :{port} or dport = :{port}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.stdout.strip()
    except Exception as exc:
        return f"ss failed: {exc}"


def _diagnose_once(args, *, verbose: bool = True) -> bool:
    base = f"http://{args.host}:{args.port}"
    status, data, raw = _http_json(f"{base}/health")
    if status == 200 and isinstance(data, dict) and data.get("ok"):
        if verbose:
            _print("OK", "bridge health", json.dumps(data, ensure_ascii=False))
            ws_url = str(data.get("ws_url") or "")
            if ".local" in ws_url and not data.get("ws_clients"):
                _print("INFO", "mdns", "ws_url uses .local; if StackChan does not connect, verify mDNS on this network")
    else:
        if verbose:
            _print("FAIL", "bridge health", f"http={status} body={raw[:300]}")

    ss_output = _ss_connections(args.port)
    if "ESTAB" in ss_output:
        if verbose:
            _print("OK", "tcp connection", "StackChan appears connected to bridge")
    elif "LISTEN" in ss_output:
        if verbose:
            _print("WARN", "tcp connection", "bridge is listening, no established StackChan connection")
    else:
        if verbose:
            _print("WARN", "tcp connection", ss_output or "no socket information")

    lines = _tail_lines(Path(args.log), args.tail)
    if not lines:
        if verbose:
            _print("WARN", "bridge log", f"missing or empty: {args.log}")
    else:
        counts = _count_markers(lines)
        if verbose:
            _print("OK", "bridge log", f"last {len(lines)} lines markers={counts}")
            for token in ("[ota]", "[ws] connected", "[listen] stop", "[asr]", "[tts]"):
                line = _last_matching(lines, token)
                if line:
                    _print("INFO", f"last {token}", line[-500:])

    if args.say:
        status, data, raw = _post_json(f"{base}/stackchan/say", {"text": args.say})
        if status == 200 and isinstance(data, dict):
            pushed = int(data.get("pushed_to_stackchan") or 0)
            if pushed:
                if verbose:
                    _print("OK", "say", json.dumps(data, ensure_ascii=False))
            else:
                if verbose:
                    _print("WARN", "say", json.dumps(data, ensure_ascii=False))
        else:
            if verbose:
                _print("FAIL", "say", f"http={status} body={raw[:300]}")

    if lines:
        counts = _count_markers(lines)
        if counts["ws_connected"] and counts["listen_stop"] and counts["asr"] and counts["tts"]:
            if verbose:
                _print("OK", "voice path evidence", "ws + listen + asr + tts markers observed")
            return True
        if counts["ws_connected"] and counts["audio"]:
            if verbose:
                _print("WARN", "voice path evidence", "device sends audio, but full ASR/TTS evidence is incomplete")
            return False
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose live StackChan Nanobot bridge status")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=12800)
    parser.add_argument("--log", default=str(ROOT / ".run" / "stackchan-bridge.log"))
    parser.add_argument("--tail", type=int, default=300)
    parser.add_argument("--say", default="", help="push a display-only test message to connected StackChan")
    parser.add_argument("--watch-seconds", type=int, default=0, help="wait up to this many seconds for full voice evidence")
    args = parser.parse_args()

    if args.watch_seconds <= 0:
        return 0 if _diagnose_once(args) else 1

    deadline = time.time() + args.watch_seconds
    _print("INFO", "watch", f"waiting up to {args.watch_seconds}s for ws + listen + asr + tts evidence")
    while time.time() < deadline:
        if _diagnose_once(args, verbose=False):
            _diagnose_once(args, verbose=True)
            return 0
        time.sleep(2)
    _diagnose_once(args, verbose=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
