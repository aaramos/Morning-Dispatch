"""Exploration build queue: serialized background builds plus cancellation.

Extracted verbatim from ``explore.py`` (M3 split); the queue worker defers its
import of ``explore`` so the two modules do not form an import cycle.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from typing import Any

from backend.app.db import database


logger = logging.getLogger(__name__)

_BUILD_QUEUE_TASK: asyncio.Task[None] | None = None
_BUILD_QUEUE_EVENT: asyncio.Event | None = None


class BuildCancelled(RuntimeError):
    pass


async def start_build_queue() -> None:
    global _BUILD_QUEUE_TASK, _BUILD_QUEUE_EVENT
    requeued = database.requeue_running_explorations()
    if requeued:
        logger.info("Requeued %s interrupted exploration build(s)", requeued)
    if _BUILD_QUEUE_TASK is None or _BUILD_QUEUE_TASK.done():
        _BUILD_QUEUE_EVENT = asyncio.Event()
        _BUILD_QUEUE_TASK = asyncio.create_task(_build_queue_worker())
    _BUILD_QUEUE_EVENT.set()


async def stop_build_queue() -> None:
    global _BUILD_QUEUE_TASK, _BUILD_QUEUE_EVENT
    if _BUILD_QUEUE_TASK is None:
        _BUILD_QUEUE_EVENT = None
        return
    _BUILD_QUEUE_TASK.cancel()
    with suppress(asyncio.CancelledError):
        await _BUILD_QUEUE_TASK
    _BUILD_QUEUE_TASK = None
    _BUILD_QUEUE_EVENT = None


def _signal_build_queue() -> None:
    if _BUILD_QUEUE_EVENT is not None:
        _BUILD_QUEUE_EVENT.set()


def cancel_exploration(exploration_id: str) -> dict[str, Any] | None:
    return database.cancel_exploration(exploration_id)


def _raise_if_cancelled(exploration_id: str) -> None:
    current = database.get_exploration(exploration_id)
    if current is None:
        return
    progress = current.get("progress") or {}
    if current.get("status") == "failed" and progress.get("cancel_requested"):
        raise BuildCancelled(str(progress.get("error") or "Build stopped by user."))


async def _build_queue_worker() -> None:
    # Deferred import: explore re-exports this module's API, so importing it at
    # module load time would create a cycle.
    from backend.app.services import explore

    while True:
        if _BUILD_QUEUE_EVENT is None:
            await asyncio.sleep(0.5)
            continue
        await _BUILD_QUEUE_EVENT.wait()
        _BUILD_QUEUE_EVENT.clear()
        while True:
            exploration = database.claim_next_queued_exploration()
            if exploration is None:
                break
            progress = dict(exploration.get("progress") or {})
            queue_options = dict(progress.get("queue_options") or {})
            try:
                raw_lh = queue_options.get("lookback_hours")
                await explore._run_exploration(
                    str(exploration["topic_id"]),
                    mode=str(exploration.get("mode") or "show_now"),
                    source_selection=dict(exploration.get("source_selection") or {}),
                    candidate_limit=int(queue_options.get("candidate_limit") or 250),
                    lookback_hours=int(raw_lh) if raw_lh is not None else None,
                    existing_exploration=exploration,
                )
            except Exception:
                logger.exception(
                    "Queued exploration %s failed",
                    exploration.get("exploration_id"),
                )
