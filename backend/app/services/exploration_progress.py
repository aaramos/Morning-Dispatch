"""Exploration progress bookkeeping and debounced persistence.

Extracted verbatim from ``explore.py`` (M3 split). Owns the in-memory progress
dict helpers used by the build pipeline plus the persistence layer that writes
progress JSON for the UI (debounced sync path for callbacks, ``asyncio.to_thread``
async path for stage boundaries — P4).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
from time import monotonic
from typing import Any

from backend.agents.discovery import default_source_registry
from backend.app.db import database


_PIPELINE_STAGES = ("discovery", "fetch", "summarize", "audit", "rank", "review", "done")


def _initial_progress(
    source_selection: dict[str, bool],
    source_names: list[str] | None = None,
) -> dict[str, Any]:
    if source_names is None:
        source_names = sorted(default_source_registry().names())
    progress_sources = {
        name: {"status": "disabled", "candidate_count": 0}
        if not bool(source_selection.get(name, False))
        else {"status": "pending", "candidate_count": 0}
        for name in source_names
    }
    return {
        "pipeline": {stage: "pending" for stage in _PIPELINE_STAGES},
        "sources": progress_sources,
        "candidate_count": 0,
        "exclusions": [],
    }


def _set_pipeline_stage(progress: dict[str, Any], stage: str, value: str) -> None:
    pipeline = dict(progress.get("pipeline", {}))
    pipeline[stage] = value
    progress["pipeline"] = pipeline


def _set_source_status(progress: dict[str, Any], adapter_status: Any) -> None:
    sources = dict(progress.get("sources", {}))
    source_status = {
        "status": adapter_status.status,
        "candidate_count": adapter_status.candidate_count,
        "message": adapter_status.message,
        "elapsed_ms": adapter_status.elapsed_ms,
        "timeout_seconds": adapter_status.timeout_seconds,
    }
    reason_code = getattr(adapter_status, "reason_code", None)
    if reason_code:
        source_status["reason_code"] = reason_code
    sources[adapter_status.name] = source_status
    progress["sources"] = sources


def _init_reasoning_bucket(progress: dict[str, Any], stage: str, persist: Callable[[], None]) -> None:
    reasoning = dict(progress.get("reasoning", {}))
    if reasoning.get(stage) is None:
        reasoning[stage] = ""
        progress["reasoning"] = reasoning
        persist()


def _reasoning_flusher(progress: dict[str, Any], persist: Callable[[], None]) -> Callable[[str], Callable[[str], None]]:
    from time import monotonic

    state: dict[str, dict[str, Any]] = {
        "editorial": {"len": 0, "last_flush": monotonic()},
        "critic": {"len": 0, "last_flush": monotonic()},
    }

    def _make_callback(stage: str) -> Callable[[str], None]:
        def _callback(chunk: str) -> None:
            if not chunk:
                return
            reasoning = dict(progress.get("reasoning", {}))
            current = str(reasoning.get(stage, ""))
            current += chunk
            reasoning[stage] = current
            progress["reasoning"] = reasoning
            now = monotonic()
            state_info = state.get(stage)
            if state_info is None:
                state[stage] = {"len": len(current), "last_flush": now}
                persist()
                return
            if len(current) - int(state_info["len"]) >= 240 or now - float(state_info["last_flush"]) >= 0.35:
                state_info["len"] = len(current)
                state_info["last_flush"] = now
                persist()

        return _callback

    return _make_callback


def _set_candidate_count(progress: dict[str, Any], value: int) -> None:
    progress["candidate_count"] = value


def _set_exclusion_reasons(progress: dict[str, Any], reasons: Any) -> None:
    if not reasons:
        return
    progress["exclusions"] = list(reasons)


# Debounce window for progress persistence (P4). Mid-stage progress updates
# (adapter callbacks, reasoning streams) can fire dozens of times per second;
# the UI polls every 2.5s, so sub-300ms granularity is invisible. Writes are
# forced (``flush=True``) at stage boundaries and before status transitions so
# nothing meaningful is ever lost.
_PROGRESS_PERSIST_MIN_INTERVAL_SECONDS = 0.3
_PROGRESS_PERSIST_STATE: dict[str, dict[str, Any]] = {}


def _progress_persist_should_write(exploration_id: str, *, flush: bool) -> bool:
    state = _PROGRESS_PERSIST_STATE.setdefault(
        exploration_id, {"last_write": float("-inf"), "dirty": False}
    )
    now = monotonic()
    if not flush and now - float(state["last_write"]) < _PROGRESS_PERSIST_MIN_INTERVAL_SECONDS:
        state["dirty"] = True
        return False
    state["last_write"] = now
    state["dirty"] = False
    return True


def _discard_progress_persist_state(exploration_id: str) -> None:
    _PROGRESS_PERSIST_STATE.pop(exploration_id, None)


def _persist_progress(exploration_id: str, progress: dict[str, Any], *, flush: bool = False) -> None:
    """Persist progress synchronously, debounced unless ``flush`` is set."""
    if not _progress_persist_should_write(exploration_id, flush=flush):
        return
    database.update_exploration_progress(
        exploration_id,
        progress=_persistable_progress(progress),
    )


async def _persist_progress_async(
    exploration_id: str, progress: dict[str, Any], *, flush: bool = False
) -> None:
    """Persist progress from async code without blocking the event loop.

    The sqlite write (and the JSON serialization of the full progress dict) runs
    in a worker thread; the progress payload is deep-copied on the loop first so
    concurrent tasks mutating ``progress`` cannot race the serialization.
    """
    if not _progress_persist_should_write(exploration_id, flush=flush):
        return
    payload = copy.deepcopy(_persistable_progress(progress))
    await asyncio.to_thread(
        database.update_exploration_progress,
        exploration_id,
        progress=payload,
    )


def _persistable_progress(progress: dict[str, Any]) -> dict[str, Any]:
    """Return the public JSON-safe progress payload.

    Internal pipeline bundles such as ``_intermediates`` can contain dataclass
    instances used by in-process reporting. They should never be written to the
    exploration progress JSON that powers the UI.
    """

    return {key: value for key, value in dict(progress).items() if not str(key).startswith("_")}
