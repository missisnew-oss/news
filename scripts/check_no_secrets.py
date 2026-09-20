#!/usr/bin/env python3
"""Fail if anything that looks like a live credential is committed.

Runs in CI (see .github/workflows/ci.yml) and is worth running locally before
every commit. Placeholders in .env.example are allowed on purpose.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = [
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

ALLOWLIST_SUBSTRINGS = (
    "put-your-", "your-", "<", "example", "xxxx", "placeholder",
)

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "out", "node_modules", ".pytest_cache"}


def tracked_files() -> list[Path]:
    try:
        output = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        return [ROOT / line for line in output.splitlines() if line]
    except Exception:
        return [
            p for p in ROOT.rglob("*")
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
        ]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        if not path.is_file() or path.suffix in {".png", ".jpg", ".jpeg", ".ico", ".ttf"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in PATTERNS:
            for match in pattern.findall(text):
                snippet = match if isinstance(match, str) else str(match)
                if any(token in snippet.lower() for token in ALLOWLIST_SUBSTRINGS):
                    continue
                findings.append(f"{path.relative_to(ROOT)}: похоже на {label}")

    if (ROOT / ".env").exists():
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"], cwd=ROOT, capture_output=True
        )
        if result.returncode != 0:
            findings.append(".env существует и НЕ игнорируется git")

    if findings:
        print("НАЙДЕНЫ ВОЗМОЖНЫЕ СЕКРЕТЫ:")
        for finding in findings:
            print("  -", finding)
        return 1
    print("Секретов в репозитории не найдено.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
