from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentDecision:
    agent: str
    target: str
    decision: str
    action: str = "none"
    confidence: float | None = None
    reason: str = ""
    model_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
