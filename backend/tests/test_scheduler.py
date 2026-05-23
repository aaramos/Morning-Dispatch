from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from backend.app.db import database
from backend.app.services import scheduler


def configure_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")
    monkeypatch.setenv("MORNING_DISPATCH_SCHEDULER_DAILY_RUN_TIME", "05:00")
    monkeypatch.setenv("MORNING_DISPATCH_SCHEDULER_TIMEZONE", "America/Los_Angeles")


def test_scheduler_runs_due_active_digest(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    digest = database.create_digest(
        {
            "name": "Scheduled Brief",
            "interest": "AI model releases",
            "schedule": "daily",
            "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
        }
    )
    started: list[str] = []
    delivered: list[str] = []

    async def fake_run_digest(digest_id: str, *, trigger: str = "manual"):
        started.append(f"{trigger}:{digest_id}")
        return {"id": "run-1", "digest_id": digest_id}

    async def fake_delivery(run):
        delivered.append(str(run["digest_id"]))

    monkeypatch.setattr(scheduler.digest_runner, "run_digest", fake_run_digest)
    monkeypatch.setattr(scheduler.email_delivery, "deliver_scheduled_digest", fake_delivery)

    count = asyncio.run(scheduler.run_due_digests_once(datetime(2026, 5, 21, tzinfo=UTC)))

    assert count == 1
    assert started == [f"scheduled:{digest['id']}"]
    assert delivered == [digest["id"]]


def test_scheduler_skips_recent_digest(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    digest = database.create_digest(
        {
            "name": "Recent Brief",
            "interest": "AI model releases",
            "schedule": "daily",
            "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
        }
    )
    database.create_ingested_run(
        digest=digest,
        payloads=[],
        article_results=[],
        lookback_hours=24,
        configured_source_count=1,
        trigger="manual",
    )
    started: list[str] = []

    async def fake_run_digest(digest_id: str, *, trigger: str = "manual"):
        started.append(f"{trigger}:{digest_id}")
        return {"id": "run-2"}

    monkeypatch.setattr(scheduler.digest_runner, "run_digest", fake_run_digest)

    count = asyncio.run(scheduler.run_due_digests_once(datetime.now(UTC) + timedelta(minutes=5)))

    assert count == 0
    assert started == []


def test_daily_scheduler_uses_fixed_morning_time(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    digest = database.create_digest(
        {
            "name": "Morning Brief",
            "interest": "AI model releases",
            "schedule": "daily",
            "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
        }
    )
    latest_run = {"completed_at": "2026-05-22T04:38:13+00:00"}

    next_run = scheduler.next_run_at(digest, latest_run)

    assert next_run.isoformat(timespec="seconds") == "2026-05-22T12:00:00+00:00"


def test_daily_scheduler_due_at_fixed_morning_time(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    digest = database.create_digest(
        {
            "name": "Morning Brief",
            "interest": "AI model releases",
            "schedule": "daily",
            "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
        }
    )
    latest_run = {"completed_at": "2026-05-22T04:38:13+00:00"}

    assert not scheduler.is_due(digest, latest_run, datetime(2026, 5, 22, 11, 59, tzinfo=UTC))
    assert scheduler.is_due(digest, latest_run, datetime(2026, 5, 22, 12, 0, tzinfo=UTC))
