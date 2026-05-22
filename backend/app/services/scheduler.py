from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.app.core.config import get_settings
from backend.app.db import database
from backend.app.services import digest_runner

logger = logging.getLogger(__name__)

SCHEDULE_HOURS = {
    "hourly": 1,
    "daily": 24,
    "weekly": 168,
    "monthly": 720,
}

_task: asyncio.Task[None] | None = None
_running_digest_ids: set[str] = set()
_last_check_at: str | None = None
_last_error: str | None = None
_last_started_count = 0


async def start_scheduler() -> None:
    global _task
    settings = get_settings()
    if not settings.scheduler_enabled or _task is not None:
        return
    _task = asyncio.create_task(_scheduler_loop(settings.scheduler_interval_seconds))


async def stop_scheduler() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None


def status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "enabled": settings.scheduler_enabled,
        "running": _task is not None and not _task.done(),
        "interval_seconds": settings.scheduler_interval_seconds,
        "daily_run_time": settings.scheduler_daily_run_time,
        "timezone": settings.scheduler_timezone,
        "last_check_at": _last_check_at,
        "last_started_count": _last_started_count,
        "last_error": _last_error,
        "running_digest_ids": sorted(_running_digest_ids),
    }


async def run_due_digests_once(now: datetime | None = None) -> int:
    global _last_check_at, _last_error, _last_started_count
    current_time = now or datetime.now(UTC)
    _last_check_at = current_time.isoformat(timespec="seconds")
    started_count = 0

    for digest in database.list_digests():
        digest_id = str(digest["id"])
        latest_run = database.get_latest_run_for_digest(digest_id)
        if not is_due(digest, latest_run, current_time):
            continue
        if digest_id in _running_digest_ids:
            continue

        _running_digest_ids.add(digest_id)
        started_count += 1
        try:
            await digest_runner.run_digest(digest_id, trigger="scheduled")
        except Exception as exc:  # pragma: no cover - scheduler must keep running.
            _last_error = f"{digest.get('name') or digest_id}: {exc}"
            logger.exception("Scheduled digest run failed for %s", digest_id)
        finally:
            _running_digest_ids.discard(digest_id)

    _last_started_count = started_count
    if started_count:
        _last_error = None
    return started_count


def is_due(digest: dict[str, Any], latest_run: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if str(digest.get("status") or "active") != "active":
        return False
    if latest_run is None:
        return True
    return next_run_at(digest, latest_run) <= (now or datetime.now(UTC))


def next_run_at(digest: dict[str, Any], latest_run: dict[str, Any] | None) -> datetime:
    latest_at = _latest_run_time(latest_run)
    if latest_at is None:
        return datetime.now(UTC)
    if str(digest.get("schedule") or "daily") == "daily":
        return _next_daily_run_after(latest_at)
    hours = SCHEDULE_HOURS.get(str(digest.get("schedule") or "daily"), 24)
    return latest_at + timedelta(hours=hours)


def _scheduler_digest_status(digest: dict[str, Any], latest_run: dict[str, Any] | None) -> dict[str, Any]:
    settings = get_settings()
    next_at = next_run_at(digest, latest_run)
    return {
        "next_run_at": next_at.isoformat(timespec="seconds"),
        "due": is_due(digest, latest_run),
        "scheduler_daily_run_time": settings.scheduler_daily_run_time,
        "scheduler_timezone": settings.scheduler_timezone,
    }


def decorate_digest_overview(overview: dict[str, Any]) -> dict[str, Any]:
    latest_run = {
        "run_at": overview.get("latest_run_at"),
        "completed_at": overview.get("latest_completed_at"),
    } if overview.get("latest_run_id") else None
    return {**overview, **_scheduler_digest_status(overview, latest_run)}


async def _scheduler_loop(interval_seconds: int) -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await run_due_digests_once()
        except Exception as exc:  # pragma: no cover - defensive guard around the loop itself.
            global _last_error
            _last_error = str(exc)
            logger.exception("Digest scheduler tick failed")
        await asyncio.sleep(interval_seconds)


def _latest_run_time(latest_run: dict[str, Any] | None) -> datetime | None:
    if not latest_run:
        return None
    raw_value = latest_run.get("completed_at") or latest_run.get("run_at")
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _next_daily_run_after(latest_at: datetime) -> datetime:
    settings = get_settings()
    run_time = _daily_run_time(settings.scheduler_daily_run_time)
    scheduler_zone = _scheduler_zone(settings.scheduler_timezone)
    latest_local = latest_at.astimezone(scheduler_zone)
    candidate = datetime.combine(latest_local.date(), run_time, tzinfo=scheduler_zone)
    if candidate <= latest_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def _daily_run_time(value: str) -> time:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, ValueError):
        return time(hour=5)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return time(hour=5)
    return time(hour=hour, minute=minute)


def _scheduler_zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
