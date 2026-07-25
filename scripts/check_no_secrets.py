#!/usr/bin/env python3
"""Reject high-confidence secrets from files staged for commit."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


TOKEN_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bsk-[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
ASSIGNMENT_PATTERN = re.compile(
    r"(?m)^\s*(?:export\s+)?[\"']?"
    r"([A-Z][A-Z0-9_]*(?:API_KEY|API_TOKEN|PRIVATE_KEY|CLIENT_SECRET|PASSWORD)"
    r"|GH_TOKEN|GITHUB_TOKEN)"
    r"[\"']?\s*[:=]\s*[\"']?([^\s\"',}#]+)"
)
SAFE_VALUE_PREFIXES = (
    "${",
    "$",
    "your-",
    "example",
    "replace",
    "changeme",
    "dummy",
    "local-no-secret",
    "local-test-",
    "offline-test-",
    "test-",
)


def findings(text: str) -> list[str]:
    result = ["high-confidence token or private key" for pattern in TOKEN_PATTERNS if pattern.search(text)]
    for match in ASSIGNMENT_PATTERN.finditer(text):
        value = match.group(2).strip().lower()
        if value and not value.startswith(SAFE_VALUE_PREFIXES):
            result.append(f"non-placeholder value assigned to {match.group(1)}")
    return result


def staged_files() -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        check=True,
        capture_output=True,
    )
    return [item.decode() for item in completed.stdout.split(b"\0") if item]


def staged_text(path: str) -> str:
    completed = subprocess.run(
        ["git", "show", f":{path}"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0 or b"\0" in completed.stdout:
        return ""
    return completed.stdout.decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="scan the Git index")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()

    if args.staged:
        candidates = ((path, staged_text(path)) for path in staged_files())
    else:
        candidates = ((path, Path(path).read_text(encoding="utf-8", errors="replace")) for path in args.paths)

    failed = False
    for path, text in candidates:
        for reason in findings(text):
            print(f"secret scan failed: {path}: {reason}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
