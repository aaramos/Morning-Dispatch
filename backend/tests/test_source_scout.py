from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from backend.agents import source_scout
from backend.app.main import create_app
from backend.app.services import source_scout as source_scout_service


def test_source_scout_promotes_and_retires_sources() -> None:
    review = source_scout.review_reddit_sources(
        digest_interest="Local AI infrastructure, agentic coding workflows, model releases, and product strategy",
        current_sources=[
            {
                "subreddit": "LocalLLaMA",
                "state": "candidate",
                "category": "Privacy & Infrastructure",
                "tags": ["local", "llm", "ollama", "models"],
                "consecutive_stale_runs": 0,
            },
            {
                "subreddit": "raspberry_pi",
                "state": "active",
                "category": "Privacy & Infrastructure",
                "tags": ["edge", "hardware"],
                "consecutive_stale_runs": 2,
            },
        ],
        observations={
            "localllama": source_scout.SourceObservation(
                subreddit="LocalLLaMA",
                sampled_posts=10,
                relevant_posts=9,
                fresh_posts=9,
                avg_comments=22,
                avg_score=80,
                sample_titles=("New local model runtime benchmark",),
            ),
            "raspberry_pi": source_scout.SourceObservation(
                subreddit="raspberry_pi",
                sampled_posts=10,
                relevant_posts=0,
                fresh_posts=4,
                avg_comments=5,
                avg_score=12,
                sample_titles=("Helpdesk post",),
            ),
        },
    )

    states = {update.subreddit: update.state for update in review.updates}
    assert states["LocalLLaMA"] == "active"
    assert states["raspberry_pi"] == "retired"
    assert any(decision.action == "promote_to_active" for decision in review.decisions)
    assert any(decision.action == "move_to_retired" for decision in review.decisions)


def test_source_scout_preserves_seed_identity_when_rediscovered() -> None:
    review = source_scout.review_reddit_sources(
        digest_interest="Local AI infrastructure and agentic coding workflows",
        current_sources=[
            {
                "subreddit": "LocalLLaMA",
                "state": "active",
                "category": "Privacy & Infrastructure",
                "tags": ["local", "llm", "ollama", "models"],
            }
        ],
        discovered_subreddits={
            "LocalLLaMA": 6,
            "ProgrammerHumor": 8,
            "NewAgentBuilders": 4,
        },
    )

    updates = {update.subreddit: update for update in review.updates}
    assert updates["LocalLLaMA"].category == "Privacy & Infrastructure"
    assert "NewAgentBuilders" in updates
    assert "ProgrammerHumor" not in updates
    assert [update.subreddit for update in review.updates].count("LocalLLaMA") == 1


def test_admin_source_scout_seeds_sources(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/digests",
            json={
                "name": "AI Morning Brief",
                "interest": "Local AI infrastructure and agentic coding workflows",
                "schedule": "daily",
                "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
            },
        )
        assert created.status_code == 201
        digest = created.json()

        scout_run = client.post(f"/api/admin/digests/{digest['id']}/source-scout?live_sample=false")
        assert scout_run.status_code == 200
        payload = scout_run.json()
        assert payload["status"] == "completed"
        assert len(payload["sources"]) >= 18
        states = {source["subreddit"]: source["state"] for source in payload["sources"]}
        assert states["LocalLLaMA"] == "active"
        assert states["raspberry_pi"] == "candidate"
        assert payload["decisions"]

        status = client.get("/api/admin/status")
        assert status.status_code == 200
        assert status.json()["source_scout"]["source_count"] >= 18

        listed = client.get("/api/admin/source-scout")
        assert listed.status_code == 200
        assert listed.json()["sources"]


def test_source_scout_retries_reddit_sampling(monkeypatch) -> None:
    browse_calls = 0
    search_calls = 0

    async def fake_browse_subreddit(*_args, **_kwargs):
        nonlocal browse_calls
        browse_calls += 1
        if browse_calls == 1:
            raise RuntimeError("temporary browse failure")
        return [
            {
                "title": "Local AI agent workflow benchmark",
                "content": "Developers compare local LLM agents, MCP tools, and coding workflows.",
                "created_utc": datetime.now(UTC).timestamp(),
                "num_comments": 12,
                "score": 55,
            }
        ]

    async def fake_search_reddit(*_args, **_kwargs):
        nonlocal search_calls
        search_calls += 1
        if search_calls == 1:
            raise RuntimeError("temporary search failure")
        return [{"subreddit": "LocalLLaMA"}, {"subreddit": "AIAgents"}]

    monkeypatch.setattr(source_scout_service, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(source_scout_service.reddit_mcp_client, "browse_subreddit", fake_browse_subreddit)
    monkeypatch.setattr(source_scout_service.reddit_mcp_client, "search_reddit", fake_search_reddit)
    monkeypatch.setattr(source_scout_service.scout_agent, "discovery_queries", lambda: ("agentic AI",))

    observations, discovered, errors = asyncio.run(
        source_scout_service._sample_reddit(
            "Local AI infrastructure and agentic coding workflows",
            [{"subreddit": "LocalLLaMA", "state": "active", "score": 0.8}],
        )
    )

    assert browse_calls == 2
    assert search_calls == 2
    assert observations["localllama"].error is None
    assert observations["localllama"].sampled_posts == 1
    assert discovered["AIAgents"] == 1
    assert errors == []


def test_source_scout_treats_empty_subreddit_as_stale_signal(monkeypatch) -> None:
    async def fake_browse_subreddit(*_args, **_kwargs):
        return []

    async def fake_search_reddit(*_args, **_kwargs):
        return []

    monkeypatch.setattr(source_scout_service, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(source_scout_service.scout_agent, "discovery_queries", lambda: ())
    monkeypatch.setattr(source_scout_service.reddit_mcp_client, "browse_subreddit", fake_browse_subreddit)
    monkeypatch.setattr(source_scout_service.reddit_mcp_client, "search_reddit", fake_search_reddit)

    observations, _discovered, errors = asyncio.run(
        source_scout_service._sample_reddit(
            "Local AI infrastructure and agentic coding workflows",
            [{"subreddit": "LMStudio", "state": "active", "score": 0.8}],
        )
    )

    assert observations["lmstudio"].sampled_posts == 0
    assert observations["lmstudio"].error is None
    assert errors == []
