from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from backend.agents.discovery.adapters import GmailSourceAdapter
from backend.agents.discovery.types import SourceAdapterContext, TopicProfile
from backend.app.db import database
from backend.app.main import create_app
from backend.app.services import gmail_allowlist


def configure_runtime(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))


# --- DB store roundtrip -------------------------------------------------------


def test_candidate_recorded_as_candidate_not_approved(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    record = database.record_gmail_sender_candidate(
        "AI@Example.com",
        sender_name="AI Weekly",
        source="refinement",
        message_count=3,
    )
    assert record is not None
    assert record["sender"] == "ai@example.com"
    assert record["state"] == "candidate"
    assert database.approved_gmail_senders() == []


def test_approved_senders_only_returns_approved(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    database.add_gmail_sender("news@one.com", state="approved")
    database.record_gmail_sender_candidate("maybe@two.com")
    database.add_gmail_sender("nope@three.com", state="rejected")

    assert database.approved_gmail_senders() == ["news@one.com"]


def test_candidate_cannot_downgrade_approved(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    database.add_gmail_sender("news@one.com", state="approved")
    # A later discovery pass surfaces the same sender again.
    again = database.record_gmail_sender_candidate("news@one.com", message_count=10)

    assert again is not None
    assert again["state"] == "approved"
    assert again["message_count"] == 10
    assert database.approved_gmail_senders() == ["news@one.com"]


def test_invalid_address_is_rejected(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    assert database.record_gmail_sender_candidate("not-an-email") is None
    assert database.add_gmail_sender("also-bad") is None


# --- service layer ------------------------------------------------------------


def test_service_record_and_approve_candidates(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    recorded = gmail_allowlist.record_candidates(
        [
            {"sender": "ai@example.com", "sender_name": "AI Weekly", "subject": "Latest"},
            {"sender": "bad-address", "sender_name": "Skip"},
        ]
    )
    assert recorded == 1
    assert database.approved_gmail_senders() == []

    approved = gmail_allowlist.approve_senders(["ai@example.com"])
    assert approved == ["ai@example.com"]
    assert database.approved_gmail_senders() == ["ai@example.com"]


def test_service_reject_and_remove_raise_for_unknown(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    import pytest

    with pytest.raises(LookupError):
        gmail_allowlist.approve_sender("ghost@nowhere.com")
    with pytest.raises(LookupError):
        gmail_allowlist.remove_sender("ghost@nowhere.com")


# --- admin endpoints ----------------------------------------------------------


def test_admin_allowlist_crud(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        empty = client.get("/api/admin/gmail/allowlist")
        assert empty.status_code == 200
        assert empty.json()["summary"]["approved_count"] == 0

        added = client.post(
            "/api/admin/gmail/allowlist",
            json={"sender": "news@example.com", "sender_name": "Example News"},
        )
        assert added.status_code == 200
        body = added.json()
        assert body["summary"]["approved_count"] == 1
        assert body["approved"][0]["sender"] == "news@example.com"

        rejected = client.post("/api/admin/gmail/allowlist/news@example.com/reject")
        assert rejected.status_code == 200
        assert rejected.json()["summary"]["rejected_count"] == 1

        approved = client.post("/api/admin/gmail/allowlist/news@example.com/approve")
        assert approved.status_code == 200
        assert approved.json()["summary"]["approved_count"] == 1

        removed = client.delete("/api/admin/gmail/allowlist/news@example.com")
        assert removed.status_code == 200
        assert removed.json()["summary"]["sender_count"] == 0


def test_admin_allowlist_invalid_sender_returns_422(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/api/admin/gmail/allowlist",
            json={"sender": "not-an-email"},
        )
    assert response.status_code == 422


def test_admin_allowlist_unknown_sender_returns_404(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/admin/gmail/allowlist/ghost@nowhere.com/approve")
    assert response.status_code == 404


# --- strict adapter behavior --------------------------------------------------


def test_gmail_adapter_returns_empty_without_approved_senders(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    # A candidate alone must never be fetched.
    database.record_gmail_sender_candidate("ai@example.com")

    adapter = GmailSourceAdapter()
    context = SourceAdapterContext(exploration_id="exp-1", lookback_hours=24)
    profile = TopicProfile(topic_id="t1", statement="anything", scope="anything")

    candidates = asyncio.run(adapter.query(profile, context))
    assert candidates == []


def test_gmail_adapter_fetches_only_approved_senders(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    database.add_gmail_sender("news@example.com", state="approved")
    database.record_gmail_sender_candidate("candidate@example.com")

    captured: dict[str, object] = {}

    async def fake_fetch_newsletters(*, digest_id, sender_allowlist, lookback_hours, db_path):
        captured["sender_allowlist"] = sender_allowlist
        captured["lookback_hours"] = lookback_hours
        return []

    import backend.agents.discovery.adapters as adapters

    monkeypatch.setattr(adapters, "fetch_newsletters", fake_fetch_newsletters)

    adapter = GmailSourceAdapter()
    context = SourceAdapterContext(exploration_id="exp-1", lookback_hours=72)
    profile = TopicProfile(topic_id="t1", statement="anything", scope="anything")

    asyncio.run(adapter.query(profile, context))
    assert captured["sender_allowlist"] == ["news@example.com"]
    assert captured["lookback_hours"] == 72
