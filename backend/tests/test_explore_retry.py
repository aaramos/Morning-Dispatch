from __future__ import annotations

import asyncio
from typing import Any
import pytest

from backend.agents.discovery import DiscoveryResult, TopicProfile, Candidate
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.digestor.base import NormalizedPayload
from backend.app.core.config import get_settings
from backend.app.db import database
from backend.app.services import explore
from backend.agents.discovery.runner import DiscoveryRunner


def configure_runtime(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    explore.ensure_runtime_dirs(get_settings())


def test_explore_retry_logic(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    # Create a topic profile
    topic = database.upsert_topic_profile(
        {
            "statement": "Frontier AI lab hardware capex spending",
            "scope": "frontier AI labs capex",
            "source_selection": {"gmail": True, "web_search": True},
            "schedule": "daily",
            "content_limits": {"target_items": 3},
        }
    )
    topic_id = topic["topic_id"]

    # Track calls
    discovery_runs = []
    digest_core_runs = []
    broaden_calls = []

    async def mock_discovery_run(self, profile, *args, **kwargs):
        # Record arguments of DiscoveryRunner.run
        discovery_runs.append({
            "profile": profile,
            "low_yield": kwargs.get("low_yield", False),
            "search_queries": list(profile.search_queries),
        })
        return DiscoveryResult(
            profile=profile,
            candidates=(),
            statuses=(),
        )

    async def mock_fetch_articles(payloads, *args, **kwargs):
        return []

    async def mock_adjudicate_dates(profile, article_results, *args, **kwargs):
        return article_results, {}

    # Mock articles to return different counts per attempt
    # Attempt 1: 1 article (low yield)
    # Attempt 2: 3 articles (satisfies target yield)
    payload_item = NormalizedPayload(
        source_type="web_search",
        source_name="Web",
        original_url="https://example.com/article1",
    )
    article1 = ArticleFetchResult(
        payload=payload_item,
        original_url="https://example.com/article1",
        final_url="https://example.com/article1",
        canonical_url="https://example.com/article1",
        title="Article 1",
        text="Sample text",
        excerpt="excerpt",
        domain="example.com",
        status="fetched",
        keywords=(),
    )
    article2 = ArticleFetchResult(
        payload=payload_item,
        original_url="https://example.com/article2",
        final_url="https://example.com/article2",
        canonical_url="https://example.com/article2",
        title="Article 2",
        text="Sample text 2",
        excerpt="excerpt 2",
        domain="example.com",
        status="fetched",
        keywords=(),
    )
    article3 = ArticleFetchResult(
        payload=payload_item,
        original_url="https://example.com/article3",
        final_url="https://example.com/article3",
        canonical_url="https://example.com/article3",
        title="Article 3",
        text="Sample text 3",
        excerpt="excerpt 3",
        domain="example.com",
        status="fetched",
        keywords=(),
    )

    async def mock_run_digest_core(
        *,
        profile: TopicProfile,
        payloads: list[Any],
        fetched_articles: list[ArticleFetchResult],
        lookback_hours: int | None = 24,
        inference_run_id: str,
        progress: dict[str, Any],
        persist: Any,
        threshold: float = 0.45,
        low_yield: bool = False,
        **kwargs: Any,
    ):
        digest_core_runs.append({
            "threshold": threshold,
            "low_yield": low_yield,
            "lookback_hours": lookback_hours,
        })
        if len(digest_core_runs) == 1:
            # Low yield
            return [article1]
        else:
            # High yield
            return [article1, article2, article3]

    async def mock_broaden_queries(profile: TopicProfile, **kwargs: Any) -> TopicProfile:
        broaden_calls.append(profile)
        # Update queries to simulate broadening
        return explore.replace(profile, search_queries=("AI capex",))

    # Apply mocks
    monkeypatch.setattr(DiscoveryRunner, "run", mock_discovery_run)
    monkeypatch.setattr(explore, "fetch_articles_for_payloads", mock_fetch_articles)
    monkeypatch.setattr(explore, "_adjudicate_dates_before_source_window_filter", mock_adjudicate_dates)
    monkeypatch.setattr(explore, "_run_digest_core", mock_run_digest_core)
    monkeypatch.setattr(explore, "broaden_queries_with_agent", mock_broaden_queries)
    monkeypatch.setattr(
        database,
        "render_ingested_issue",
        lambda *_args, **_kwargs: "<html><body>explore retry test</body></html>",
    )

    # Execute
    result = asyncio.run(
        explore.run_scheduled(
            topic_id,
            source_selection={"gmail": True, "web_search": True},
            lookback_hours=48,
        )
    )

    assert result is not None
    exploration = result["exploration"]
    assert exploration["status"] == "complete"

    # Verify retry behavior
    assert len(discovery_runs) == 2
    assert len(digest_core_runs) == 2
    assert len(broaden_calls) == 1

    # First attempt: standard settings
    assert discovery_runs[0]["low_yield"] is False
    assert digest_core_runs[0]["low_yield"] is False
    assert digest_core_runs[0]["threshold"] == 0.45
    assert digest_core_runs[0]["lookback_hours"] == 48

    # Second attempt (retry): low yield mode and relaxed settings
    assert discovery_runs[1]["low_yield"] is True
    assert digest_core_runs[1]["low_yield"] is True
    assert digest_core_runs[1]["threshold"] == 0.30
    # Strict constraint: lookback_hours stays fixed
    assert digest_core_runs[1]["lookback_hours"] == 48

    # Check query was broadened
    assert discovery_runs[0]["search_queries"] == []
    assert discovery_runs[1]["search_queries"] == ["AI capex"]


def test_candidate_matches_topic_low_yield() -> None:
    from backend.agents.discovery.runner import _candidate_matches_topic

    topic_tokens = {"semiconductor", "capex", "nvidia", "hardware", "spending"}
    
    # Candidate with 1 overlap token ("nvidia") in title
    cand1 = Candidate(
        adapter="web_search",
        payload=NormalizedPayload(
            source_type="web_search",
            source_name="Web",
            original_url="https://example.com/article1",
            metadata={"title": "Nvidia chip orders"},
        ),
    )
    
    # When low_yield=False, 1 overlap token is not enough
    assert _candidate_matches_topic(cand1, topic_tokens, low_yield=False) is False
    # When low_yield=True, overlap of 1 is accepted
    assert _candidate_matches_topic(cand1, topic_tokens, low_yield=True) is True


@pytest.mark.anyio
async def test_screen_candidates_prompt_relaxation(monkeypatch) -> None:
    from backend.agents.discovery.query_refiner import screen_candidates

    system_prompts_seen = []
    
    class FakeClient:
        async def complete_json(self, system: str, prompt: str, max_tokens: int = 2000):
            system_prompts_seen.append(system)
            return {"decisions": []}
            
    class FakeResolution:
        client = FakeClient()
        
    from backend.app.services import model_routing
    monkeypatch.setattr(model_routing, "client_for_agent", lambda *args, **kwargs: FakeResolution())
    
    profile = TopicProfile(
        topic_id="test",
        statement="AI chip capex",
        scope="AI capex",
    )
    
    cand = Candidate(
        adapter="gmail",
        payload=NormalizedPayload(
            source_type="gmail",
            source_name="Gmail",
            original_url="https://example.com/gmail",
        ),
    )
    
    await screen_candidates(profile, [cand], low_yield=True)
    assert len(system_prompts_seen) == 1
    assert "CRITICAL: We are in a low-yield retrieval mode." in system_prompts_seen[0]
    
    system_prompts_seen.clear()
    await screen_candidates(profile, [cand], low_yield=False)
    assert len(system_prompts_seen) == 1
    assert "CRITICAL: We are in a low-yield retrieval mode." not in system_prompts_seen[0]


@pytest.mark.anyio
async def test_screen_candidates_is_bounded_and_fails_open(monkeypatch) -> None:
    from backend.agents.discovery import query_refiner

    calls = []

    class SlowClient:
        async def complete_json(self, system: str, prompt: str, max_tokens: int = 2000):
            calls.append(prompt)
            await asyncio.sleep(0.05)
            return {"decisions": [{"id": "gmail-0", "decision": "drop"}]}

    class FakeResolution:
        client = SlowClient()

    from types import SimpleNamespace
    from backend.app.services import model_routing
    monkeypatch.setattr(model_routing, "client_for_agent", lambda *args, **kwargs: FakeResolution())
    monkeypatch.setattr(query_refiner, "get_settings", lambda: SimpleNamespace(model_timeout_seconds=0.0))
    monkeypatch.setattr(query_refiner, "_SCREENING_MAX_CANDIDATES_PER_SOURCE", 2)
    monkeypatch.setattr(query_refiner, "_SCREENING_BATCH_SIZE", 1)
    monkeypatch.setattr(query_refiner, "_SCREENING_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(query_refiner, "_SCREENING_BATCH_TIMEOUT_SECONDS", 0.01)

    profile = TopicProfile(
        topic_id="test",
        statement="AI chip capex",
        scope="AI capex",
    )
    candidates = [
        Candidate(
            adapter="gmail",
            score=1.0 - (index * 0.1),
            payload=NormalizedPayload(
                id=f"gmail-{index}",
                source_type="gmail",
                source_name="Gmail",
                original_url=f"https://example.com/gmail/{index}",
                metadata={"subject": f"Gmail item {index}"},
            ),
        )
        for index in range(5)
    ]

    screened = await query_refiner.screen_candidates(profile, candidates)

    assert len(screened) == 5
    assert len(calls) == 2


@pytest.mark.anyio
async def test_source_audit_prompt_relaxation(monkeypatch) -> None:
    from backend.agents.source_audit import apply_source_audit

    system_prompts_seen = []
    
    class FakeClient:
        async def complete_json(self, system: str, prompt: str, max_tokens: int = 1600):
            system_prompts_seen.append(system)
            return {"decisions": [], "summary": "mock summary"}
            
    profile = TopicProfile(
        topic_id="test",
        statement="AI chip capex",
        scope="AI capex",
    )
    
    payload = NormalizedPayload(
        source_type="web_search",
        source_name="Web",
        original_url="https://example.com/article1",
    )
    article = ArticleFetchResult(
        payload=payload,
        original_url="https://example.com/article1",
        final_url="https://example.com/article1",
        canonical_url="https://example.com/article1",
        title="Article 1",
        text="Sample text",
        excerpt="excerpt",
        domain="example.com",
        status="fetched",
        keywords=(),
        relevance_score=0.8,
    )
    
    await apply_source_audit(profile, [article], lookback_hours=24, model_client=FakeClient(), low_yield=True)
    assert len(system_prompts_seen) == 1
    assert "CRITICAL: We are in a low-yield retrieval mode." in system_prompts_seen[0]
    
    system_prompts_seen.clear()
    await apply_source_audit(profile, [article], lookback_hours=24, model_client=FakeClient(), low_yield=False)
    assert len(system_prompts_seen) == 1
    assert "CRITICAL: We are in a low-yield retrieval mode." not in system_prompts_seen[0]
