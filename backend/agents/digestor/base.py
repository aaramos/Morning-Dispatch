from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class NormalizedPayload:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_type: str = "gmail"
    source_name: str = ""
    raw_text: str = ""
    original_url: str | None = None
    published_at: str | None = None
    fetched_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


PII_KEYWORDS = [
    "password",
    "passwd",
    "ssn",
    "social security",
    "account number",
    "routing number",
    "credit card",
    "bank account",
    "tax id",
    "driver's license",
]


def pii_filter(payload: NormalizedPayload) -> bool:
    text = payload.raw_text.lower()
    return not any(keyword in text for keyword in PII_KEYWORDS)
