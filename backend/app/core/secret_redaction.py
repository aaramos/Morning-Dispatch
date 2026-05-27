from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [redacted]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[redacted-google-api-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[redacted-openai-key]"),
    (re.compile(r"\btvly-[A-Za-z0-9_-]{20,}\b"), "[redacted-tavily-key]"),
    (re.compile(r"\bBSA[A-Za-z0-9_-]{20,}\b"), "[redacted-brave-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "[redacted-github-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted-aws-key]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|client[_-]?secret|secret|token|authorization|password)"
            r"\b(\s*[:=]\s*)['\"]?[^'\"\s,}]{8,}"
        ),
        r"\1\2[redacted]",
    ),
)


def redact_secret_text(value: str) -> str:
    redacted = value
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secret_text(value)
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    return value


def looks_like_secret(value: str) -> bool:
    return redact_secret_text(value) != value
