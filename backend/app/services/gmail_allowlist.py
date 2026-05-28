"""Curation surface for the strict Gmail sender allowlist.

The build pipeline only ever fetches newsletters from senders in the ``approved``
state. Refinement and discovery may surface new senders, but they land as
``candidate`` records that a human must explicitly approve before they can ever
appear in a brief.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from backend.app.db import database

logger = logging.getLogger(__name__)


def allowlist_status() -> dict[str, Any]:
    senders = database.list_gmail_senders()
    return {
        "summary": database.gmail_allowlist_summary(),
        "approved": [sender for sender in senders if sender.get("state") == "approved"],
        "candidates": [sender for sender in senders if sender.get("state") == "candidate"],
        "rejected": [sender for sender in senders if sender.get("state") == "rejected"],
    }


def add_sender(sender: str, *, sender_name: str | None = None) -> dict[str, Any]:
    record = database.add_gmail_sender(
        sender,
        sender_name=sender_name,
        state="approved",
        source="manual",
    )
    if record is None:
        raise ValueError("A valid email address is required.")
    return allowlist_status()


def approve_sender(sender: str) -> dict[str, Any]:
    if database.set_gmail_sender_state(sender, "approved") is None:
        raise LookupError(f"Unknown Gmail sender: {sender}")
    return allowlist_status()


def reject_sender(sender: str) -> dict[str, Any]:
    if database.set_gmail_sender_state(sender, "rejected") is None:
        raise LookupError(f"Unknown Gmail sender: {sender}")
    return allowlist_status()


def remove_sender(sender: str) -> dict[str, Any]:
    if not database.delete_gmail_sender(sender):
        raise LookupError(f"Unknown Gmail sender: {sender}")
    return allowlist_status()


def record_candidates(candidates: Iterable[dict[str, Any]], *, source: str = "refinement") -> int:
    """Persist discovered senders as candidates pending approval. Returns count recorded."""
    recorded = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        sender = str(candidate.get("sender") or "").strip().lower()
        if "@" not in sender:
            continue
        record = database.record_gmail_sender_candidate(
            sender,
            sender_name=str(candidate.get("sender_name") or "").strip() or None,
            source=source,
            reason=str(candidate.get("subject") or "").strip() or None,
            message_count=_safe_int(candidate.get("message_count")),
            last_seen_at=str(candidate.get("latest_at") or "").strip() or None,
        )
        if record is not None:
            recorded += 1
    return recorded


def approve_senders(senders: Iterable[str], *, source: str = "refinement") -> list[str]:
    """Approve a set of senders into the persistent allowlist, creating records as needed."""
    approved: list[str] = []
    for sender in senders:
        address = str(sender or "").strip().lower()
        if "@" not in address:
            continue
        record = database.add_gmail_sender(address, state="approved", source=source)
        if record is not None:
            approved.append(address)
    return approved


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
