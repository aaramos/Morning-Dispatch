#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    ".uv-cache",
    ".npm-cache",
    "node_modules",
    "frontend/dist",
    ".pytest_cache",
    ".playwright-cache",
    ".playwright-cli",
    ".playwright-home",
}
ALLOWLIST_FILES = {".env.example"}
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Tavily API key", re.compile(r"\btvly-[A-Za-z0-9_-]{20,}\b")),
    ("Brave Search key", re.compile(r"\bBSA[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Generic fallback: variable/env-var name ending with a secret-suggestive suffix
    # assigned a long opaque value. Catches Podcast Index, SerpAPI, and other
    # unprefixed keys regardless of prefix convention.
    # Value pattern excludes underscores to avoid matching Python variable names
    # on the right-hand side (e.g. "tavily_api_key=web_search_tavily_api_key").
    (
        "generic API key assignment",
        re.compile(
            r"(?i)(?:[A-Za-z0-9_]*(?:api[_-]?key|api[_-]?secret|client[_-]?secret"
            r"|access[_-]?token|auth[_-]?token|secret[_-]?key|private[_-]?key))"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9+/=\-]{24,}['\"]?"
        ),
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan repository files for likely secrets.")
    parser.add_argument("--staged", action="store_true", help="Scan files staged for commit.")
    args = parser.parse_args()

    findings = []
    for path in _candidate_files(staged=args.staged):
        findings.extend(_scan_file(path))

    if findings:
        print("Secret scan blocked this change. Remove or rotate these values:", file=sys.stderr)
        for path, line_number, label in findings:
            rel = path.relative_to(ROOT)
            print(f"- {rel}:{line_number} looks like {label}", file=sys.stderr)
        return 1
    return 0


def _candidate_files(*, staged: bool) -> list[Path]:
    if staged:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    else:
        result = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True, text=True, capture_output=True)
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    paths = [ROOT / name for name in names]
    return [path for path in paths if path.is_file() and not _is_skipped(path)]


def _scan_file(path: Path) -> list[tuple[Path, int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    except OSError:
        return []

    findings: list[tuple[Path, int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _line_is_allowlisted(line):
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append((path, line_number, label))
                break
    return findings


def _is_skipped(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if rel.as_posix() in ALLOWLIST_FILES:
        return True
    return any(rel.as_posix() == folder or rel.as_posix().startswith(f"{folder}/") for folder in SKIP_DIRS)


def _line_is_allowlisted(line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in ("example", "placeholder", "paste api key", "your-api-key"))


if __name__ == "__main__":
    raise SystemExit(main())
