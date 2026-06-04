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
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))


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

    async def fake_run_digest(digest_id: str, *, trigger: str = "manual", skip_if_running: bool = False):
        started.append(f"{trigger}:{digest_id}")
        assert skip_if_running is True
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

    async def fake_run_digest(digest_id: str, *, trigger: str = "manual", skip_if_running: bool = False):
        started.append(f"{trigger}:{digest_id}")
        return {"id": "run-2"}

    monkeypatch.setattr(scheduler.digest_runner, "run_digest", fake_run_digest)

    count = asyncio.run(scheduler.run_due_digests_once(datetime.now(UTC) + timedelta(minutes=5)))

    assert count == 0
    assert started == []


def test_scheduler_skips_digest_with_manual_run_in_progress(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    digest = database.create_digest(
        {
            "name": "Manual Brief",
            "interest": "AI model releases",
            "schedule": "daily",
            "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
        }
    )
    started: list[str] = []
    delivered: list[str] = []

    async def fake_run_digest(digest_id: str, *, trigger: str = "manual", skip_if_running: bool = False):
        started.append(f"{trigger}:{digest_id}")
        return {"id": "run-3", "digest_id": digest_id}

    async def fake_delivery(run):
        delivered.append(str(run["digest_id"]))

    monkeypatch.setattr(scheduler.digest_runner, "is_digest_running", lambda digest_id: digest_id == digest["id"])
    monkeypatch.setattr(scheduler.digest_runner, "run_digest", fake_run_digest)
    monkeypatch.setattr(scheduler.email_delivery, "deliver_scheduled_digest", fake_delivery)

    count = asyncio.run(scheduler.run_due_digests_once(datetime(2026, 5, 21, tzinfo=UTC)))

    assert count == 0
    assert started == []
    assert delivered == []


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


def test_scheduler_runs_due_topic_profiles(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = database.upsert_topic_profile(
        {
            "statement": "Explore local AI news",
            "scope": "Local AI and tools",
            "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
            "schedule": "hourly",
            "delivery_config": {"email_enabled": True},
        }
    )

    started: list[str] = []
    scheduled_exploration_ids: list[str] = []
    delivered: list[str] = []

    async def fake_run_scheduled(topic_id: str, source_selection: dict[str, bool] | None = None):
        started.append(topic_id)
        exploration = database.create_exploration(topic_id=topic_id, mode="scheduled", source_selection=source_selection or {})
        database.update_exploration_status(exploration["exploration_id"], status="complete", brief_ref="/tmp/fake-brief.html")
        scheduled_exploration_ids.append(str(exploration["exploration_id"]))
        return {"exploration": {"exploration_id": exploration["exploration_id"]}}

    def fake_send_exploration_brief(exploration_id: str, recipient_email: str | None = None) -> dict[str, str]:
        delivered.append(exploration_id)
        return {"status": "sent"}

    monkeypatch.setattr(scheduler.explore, "run_scheduled", fake_run_scheduled)
    monkeypatch.setattr(scheduler.email_delivery, "send_exploration_brief", fake_send_exploration_brief)

    count = asyncio.run(scheduler.run_due_digests_once(datetime(2026, 5, 22, 12, 0, tzinfo=UTC)))

    assert count == 1
    assert started == [topic["topic_id"]]
    assert len(delivered) == 1
    assert len(scheduled_exploration_ids) == 1
    assert delivered == scheduled_exploration_ids


def test_scheduler_pauses_topic_email_after_first_delivery_failure(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = database.upsert_topic_profile(
        {
            "statement": "Explore local AI news",
            "scope": "Local AI and tools",
            "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
            "schedule": "hourly",
            "delivery_config": {"email_enabled": True},
        }
    )

    started: list[str] = []
    sent: list[str] = []

    async def fake_run_scheduled(topic_id: str, source_selection: dict[str, bool] | None = None):
        started.append(topic_id)
        exploration = database.create_exploration(topic_id=topic_id, mode="scheduled", source_selection=source_selection or {})
        database.update_exploration_status(exploration["exploration_id"], status="complete", brief_ref="/tmp/fake-brief.html")
        return {"exploration": {"exploration_id": exploration["exploration_id"]}}

    def fake_send_exploration_brief(exploration_id: str, recipient_email: str | None = None) -> dict[str, str]:
        sent.append(exploration_id)
        return {"status": "failed", "error": "Gmail send permission is missing."}

    monkeypatch.setattr(scheduler.explore, "run_scheduled", fake_run_scheduled)
    monkeypatch.setattr(scheduler.email_delivery, "send_exploration_brief", fake_send_exploration_brief)
    monkeypatch.setattr(scheduler, "is_topic_due", lambda *_args, **_kwargs: True)

    count = asyncio.run(scheduler.run_due_digests_once(datetime(2026, 5, 22, 12, 0, tzinfo=UTC)))

    assert count == 1
    assert len(started) == 1
    assert len(sent) == 1
    updated = database.get_topic_profile(topic["topic_id"])
    delivery_config = updated["profile"]["delivery_config"]
    assert delivery_config["delivery_disabled_after_failure"] is True
    assert delivery_config["last_delivery_status"] == "failed"
    assert delivery_config["last_error"] == "Gmail send permission is missing."

    started.clear()
    sent.clear()
    count = asyncio.run(scheduler.run_due_digests_once(datetime(2026, 5, 22, 14, 0, tzinfo=UTC)))

    assert count == 1
    assert len(started) == 1
    assert sent == []


def test_topic_due_checks_hourly_interval(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = database.upsert_topic_profile(
        {
            "statement": "Explore local AI news",
            "scope": "Hourly interval check",
            "schedule": "hourly",
        }
    )

    latest_run = {"finished_at": "2026-05-22T10:00:00+00:00"}

    assert scheduler.is_topic_due(topic, latest_run, datetime(2026, 5, 22, 10, 30, tzinfo=UTC)) is False
    assert scheduler.is_topic_due(topic, latest_run, datetime(2026, 5, 22, 11, 0, tzinfo=UTC)) is True
