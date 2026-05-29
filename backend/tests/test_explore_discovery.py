from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import time
from typing import Any
import pytest

from fastapi.testclient import TestClient

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.discovery.runner import DiscoveryRunner
from backend.agents.discovery.registry import SourceRegistry
from backend.agents.discovery.types import (
    AdapterStatus,
    Candidate,
    CostProfile,
    DiscoveryResult,
    SourceAdapterContext,
    TopicProfile,
)
from backend.app.db import database
from backend.app.main import create_app
from backend.app.services import email_delivery, explore, refinement


class FakeAdapter:
    def __init__(self, name: str, candidates: list[Candidate], *, timeout_seconds: float = 1.0):
        self.name = name
        self.cost_profile = CostProfile(label="fake", timeout_seconds=timeout_seconds)
        self.good_for = ("test",)
        self._candidates = candidates

    async def query(self, *_args, **_kwargs) -> list[Candidate]:
        return self._candidates

    async def fetch(self, candidate: Candidate) -> NormalizedPayload:
        return candidate.payload


def test_rebuild_repairs_exclusions_without_fabricating_entities():
    profile = TopicProfile.from_dict(
        {
            "topic_id": "topic-memory",
            "statement": (
                "As an investor I'm interested in Micron, Hynix, Kioxia, and sandisk. "
                "Track news from the previous 3 days and avoid MSN or Yahoo news."
            ),
            "scope": "Track company performance.",
            "search_queries": ["memory market company performance"],
            "source_selection": {"web_search": True, "markets": True},
        }
    )

    strengthened = explore._strengthen_profile_for_run(profile)

    # Generic, source-agnostic repair: excluded publishers are detected from the statement.
    assert "MSN" in strengthened.exclusions
    assert "Yahoo News" in strengthened.exclusions
    # No hardcoded tickers or company names are fabricated.
    assert "markets" not in strengthened.source_queries
    assert strengthened.search_queries == profile.search_queries


class SlowAdapter(FakeAdapter):
    async def query(self, *_args, **_kwargs) -> list[Candidate]:
        await asyncio.sleep(0.05)
        return self._candidates


def article_for_window(
    *,
    title: str,
    url: str,
    published_at: str | None = None,
    source_type: str = "gmail_link",
    summary: str = "Fresh enough coverage of the requested topic.",
) -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type=source_type,
        source_name=title,
        original_url=url,
        raw_text=summary,
        published_at=published_at,
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=url,
        final_url=url,
        canonical_url=url,
        title=title,
        text=summary,
        excerpt=summary,
        editor_summary=summary,
        domain="example.com",
        status="fetched",
    )


def configure_runtime(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))


def test_topic_profile_infers_numeric_lookback_from_interest_text() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "Track Micron, Hynix, Kioxia and Sandisk news from the previous 3 days.",
            "scope": "Memory company performance",
        }
    )

    assert profile.lookback_hours == 72


def test_refinement_interprets_year_old_source_scope_answer() -> None:
    profile = {
        "statement": "traveling to mexico city",
        "scope": "Mexico City travel planning",
        "recency_weighting": "recent",
        "lookback_hours": 72,
    }

    updated = refinement._apply_answer(profile, "recency_weighting", "articles should be no more than a year old")

    assert updated["source_scope_answered"] is True
    assert updated["recency_weighting"] == "last_year"
    assert updated["lookback_hours"] == 8760


def test_refinement_seeds_year_lookback_from_interest_text() -> None:
    profile = refinement._seed_profile_with_hints(
        {
            "statement": "traveling to mexico city; include content from the most recent year",
            "source_selection": {"web_search": True},
        }
    )

    assert profile["source_scope_answered"] is True
    assert profile["recency_weighting"] == "last_year"
    assert profile["lookback_hours"] == 8760


def test_source_window_filter_allows_undated_web_results_once(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    profile = TopicProfile.from_dict(
        {
            "topic_id": "topic-memory",
            "statement": "Track Micron, Hynix, Kioxia and Sandisk news from the previous 3 days.",
            "scope": "Memory company performance",
        }
    )
    fresh = article_for_window(
        title="Fresh Micron catalyst",
        url="https://example.com/news/fresh-micron",
        published_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds"),
    )
    stale_url = article_for_window(
        title="Old Kioxia roadmap",
        url="https://example.com/news/2025/09/16/kioxia-roadmap",
        summary="An old Kioxia roadmap story.",
    )
    undated = article_for_window(
        title="Undated Hynix market note",
        url="https://example.com/news/hynix-market-note",
        summary="No publish date is available on this search result.",
    )

    kept, issues = explore._apply_source_window_filter(
        profile,
        [fresh, stale_url, undated],
        lookback_hours=72,
    )

    assert kept[0] == fresh
    assert kept[1].title == undated.title
    assert kept[1].metadata["served_once"] is True
    assert len(issues) == 1
    assert "outside the requested source window" in issues[0]["reason"]

    database.record_served_undated_items(
        profile.topic_id,
        explore._served_undated_items_from_results(kept),
    )
    kept_again, issues_again = explore._apply_source_window_filter(
        profile,
        [undated],
        lookback_hours=72,
    )
    assert kept_again == []
    assert "already shown once" in issues_again[0]["reason"]


def test_source_window_filter_marks_undated_strict_types_as_served_once(monkeypatch, tmp_path) -> None:
    """Undated gmail_link articles are kept on first appearance but tagged served_once.

    With recency_weighting="breaking" and lookback_hours=24, the filter runs and undated
    strict-type articles (gmail_link, foreign_web, reddit_thread, podcast_episode) are
    allowed through exactly once, tagged with served_once=True metadata.
    """
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    profile = TopicProfile.from_dict(
        {
            "statement": "Track local AI tooling news.",
            "scope": "Local AI tooling",
            "recency_weighting": "breaking",
        }
    )
    undated = article_for_window(
        title="Undated local AI story",
        url="https://example.com/news/local-ai-story",
    )

    kept, issues = explore._apply_source_window_filter(profile, [undated], lookback_hours=24)

    assert len(kept) == 1
    assert kept[0].title == "Undated local AI story"
    assert kept[0].metadata.get("served_once") is True
    assert issues == []


class _TestStreamingModelClient:
    def __init__(self) -> None:
        self.config = type("Config", (), {"model": "streaming-test-model"})()
        self.calls: list[dict[str, object]] = []
        self.payloads = [
            {
                "title": "AI Deployment Report",
                "summary": "Practical AI deployment updates from a local tooling run.",
                "keywords": ["ai", "deployment", "tooling"],
                "content_type": "article",
            },
            {
                "title": "AI Agent Pipelines Guide",
                "summary": "Practical guide for AI operations and pipelines.",
                "keywords": ["ai", "pipelines", "agents"],
                "content_type": "article",
            },
            {
                "decisions": [
                    {
                        "index": 0,
                        "decision": "include",
                        "confidence": 0.9,
                        "constraint_failures": [],
                        "reason": "Fresh and on topic.",
                    },
                    {
                        "index": 1,
                        "decision": "include",
                        "confidence": 0.86,
                        "constraint_failures": [],
                        "reason": "Fresh and on topic.",
                    },
                ],
                "summary": "Both sources pass audit.",
            },
            {
                "decisions": [
                    {
                        "index": 0,
                        "decision": "lead",
                        "section": "AI Infrastructure",
                        "confidence": 0.89,
                        "reason": "Strong practitioner signal and fresh signal.",
                    },
                    {
                        "index": 1,
                        "decision": "include",
                        "section": "Models & Labs",
                        "confidence": 0.75,
                        "reason": "Solid backup signal.",
                    },
                ]
            },
            {
                "publishable": True,
                "summary": "Both stories are relevant and high quality.",
                "findings": [],
            },
        ]
        self.tokens = [
            ["Refine", " reasoning ", "chunk ", "A."],
            ["Refine", " reasoning ", "chunk ", "B."],
            ["Audit", " reasoning ", "chunk."],
            ["Editorial", " reasoning ", "chunk ", "one."],
            ["Critic", " reasoning ", "chunk ", "two."],
        ]

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        callback = kwargs.get("on_token")
        call_index = len(self.calls)
        self.calls.append(kwargs)
        for chunk in self.tokens[call_index]:
            if callable(callback):
                callback(str(chunk))
        return self.payloads[call_index]


def candidate(adapter: str, url: str, score: float) -> Candidate:
    return Candidate(
        adapter=adapter,
        payload=NormalizedPayload(
            source_type=f"{adapter}_item",
            source_name=adapter,
            original_url=url,
            raw_text=f"{adapter} candidate",
        ),
        score=score,
    )


def test_topic_profile_and_exploration_tables(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    profile = database.upsert_topic_profile(
        {
            "statement": "Track local AI infrastructure",
            "scope": "Local AI inference and Apple Silicon tools",
            "depth": "practitioner",
            "recency_weighting": "balanced",
            "exclusions": ["consumer chatbot gossip"],
            "source_selection": {"gmail": True, "reddit": False, "podcasts": True, "web_search": True},
            "models": {"refinement": None, "brief": None},
            "schedule": None,
        }
    )

    assert profile["profile"]["topic_id"] == profile["topic_id"]
    assert profile["profile"]["scope"] == "Local AI inference and Apple Silicon tools"

    exploration = database.create_exploration(
        topic_id=profile["topic_id"],
        mode="show_now",
        source_selection={"gmail": True, "reddit": False},
        status="queued",
    )
    assert exploration["status"] == "queued"
    assert exploration["source_selection"]["reddit"] is False

    updated = database.update_exploration_status(exploration["exploration_id"], status="complete")
    assert updated is not None
    assert updated["status"] == "complete"
    assert updated["finished_at"]


def test_completed_exploration_clears_running_queue_marker(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    profile = database.upsert_topic_profile(
        {
            "statement": "Track local AI infrastructure",
            "scope": "Local AI inference and Apple Silicon tools",
            "source_selection": {"web_search": True},
        }
    )
    exploration = database.create_exploration(
        topic_id=profile["topic_id"],
        mode="show_now",
        source_selection={"web_search": True},
        status="running",
    )
    database.update_exploration_progress(
        exploration["exploration_id"],
        progress={"queue": {"status": "running", "message": "Building now.", "action": "build"}},
    )

    completed = database.update_exploration_status(exploration["exploration_id"], status="complete")

    assert completed is not None
    assert completed["status"] == "complete"
    assert completed["progress"]["queue"]["status"] == "complete"
    assert completed["progress"]["queue"]["message"] == "Brief ready."


def test_run_digest_core_emits_editorial_and_critic_reasoning(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "true")
    model_client = _TestStreamingModelClient()
    monkeypatch.setattr(
        "backend.agents.model.ModelClient.from_settings",
        lambda *_args, **_kwargs: model_client,
    )

    profile = TopicProfile.from_dict(
        {
            "topic_id": "test-topic",
            "statement": "Explore practical AI tooling",
            "scope": "AI deployment and agent pipelines",
            "depth": "practitioner",
        }
    )
    payloads = [
        NormalizedPayload(
            id="payload-1",
            source_type="web_search",
            source_name="web_search",
            original_url="https://example.com/alpha",
            raw_text="Practical AI deployment report with model updates and local tooling.",
            published_at="2026-01-01T12:00:00+00:00",
            metadata={"search_query": "AI deployment"},
        ),
        NormalizedPayload(
            id="payload-2",
            source_type="web_search",
            source_name="web_search",
            original_url="https://example.com/beta",
            raw_text="Another practical guide on AI agent pipelines for operators.",
            published_at="2026-01-01T13:00:00+00:00",
            metadata={"search_query": "AI operations"},
        ),
    ]
    fetched_articles = [
        ArticleFetchResult(
            payload=payload,
            original_url=payload.original_url,
            final_url=payload.original_url,
            title=f"Article {index}",
            text=payload.raw_text,
            excerpt=payload.raw_text[:200],
            domain="example.com",
            status="fetched",
            link_score=0.91 - (index * 0.01),
            section="Models & Labs",
            editor_summary=payload.raw_text,
        )
        for index, payload in enumerate(payloads)
    ]
    progress = explore._initial_progress({"gmail": True, "reddit": True, "podcasts": True, "web_search": True})

    article_results = asyncio.run(
        explore._run_digest_core(
            profile=profile,
            payloads=payloads,
            fetched_articles=fetched_articles,
            lookback_hours=72,
            inference_run_id="explore-123",
            progress=progress,
            persist=lambda: None,
        )
    )

    assert len(article_results) == 2
    assert "Editorial" in (progress.get("reasoning", {}).get("editorial") or "")
    assert "Critic" in (progress.get("reasoning", {}).get("critic") or "")
    assert progress.get("pipeline", {}).get("audit") == "done"
    assert progress.get("source_audit", {}).get("status") == "completed"
    assert progress.get("pipeline", {}).get("review") == "done"
    assert len(model_client.calls) >= 3


def test_show_now_run_does_not_send_email_automatically(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    class _NeverCalledGmailService:
        def __call__(self) -> None:
            raise AssertionError("Email transport must not be invoked for show-now runs")

    monkeypatch.setattr(
        email_delivery,
        "_gmail_service",
        _NeverCalledGmailService(),
    )

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        profile = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore local AI workflows",
                "scope": "Local AI operations and tooling",
                "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
            },
        )
        topic_id = profile.json()["topic_id"]

        run = client.post(
            f"/api/explore/topic-profiles/{topic_id}/run",
            json={"source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False}},
        )
        assert run.status_code == 202
        exploration = run.json()["exploration"]
        assert exploration["mode"] == "show_now"
        exploration_id = exploration["exploration_id"]

        poll = client.get(f"/api/explore/explorations/{exploration_id}")
        body: dict[str, object]
        for _ in range(120):
            body = poll.json()
            if body["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
            poll = client.get(f"/api/explore/explorations/{exploration_id}")

        assert body["status"] == "complete"
        assert body["emailed"] is False
        assert isinstance(body["progress"].get("brief"), dict)
        html_path = body["progress"].get("brief", {}).get("html_path")
        assert html_path

        rendered = client.get(html_path)
        assert rendered.status_code == 200
        assert "No source content was found for this brief." in rendered.text


def test_create_topic_profile_and_queue_build_is_atomic(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        refinement = client.post(
            "/api/explore/refinement-sessions",
            json={
                "statement": "Explore durable queued briefs",
                "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
            },
        ).json()
        response = client.post(
            "/api/explore/topic-profiles/build",
            json={
                "statement": "Explore durable queued briefs",
                "scope": "Build should survive navigation",
                "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
                "refinement_session_id": refinement["session_id"],
            },
        )

        assert response.status_code == 202
        body = response.json()
        topic_id = body["topic_profile"]["topic_id"]
        exploration = body["exploration"]
        assert exploration["topic_id"] == topic_id
        assert exploration["status"] in {"queued", "running", "complete"}

        saved_topic = client.get(f"/api/explore/topic-profiles/{topic_id}")
        saved_exploration = client.get(f"/api/explore/explorations/{exploration['exploration_id']}")
        assert saved_topic.status_code == 200
        assert saved_exploration.status_code == 200
        assert database.get_refinement_session(refinement["session_id"]) is None


def test_update_topic_profile_content_limits(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore local AI workflows",
                "scope": "Local AI operations and tooling",
                "source_selection": {"web_search": True, "reddit": True},
            },
        )
        topic_id = created.json()["topic_id"]

        updated = client.post(
            f"/api/explore/topic-profiles/{topic_id}/content-limits",
            json={
                "content_limits": {
                    "total_items": 9,
                    "target_items": 6,
                    "lead_items": 2,
                    "per_source": {"web_search": 5, "reddit": 2},
                    "quality_floor": "strong",
                },
                "lookback_hours": 168,
                "pipeline_limits": {
                    "article_fetches": 90,
                    "article_fetch_concurrency": 4,
                    "model_refinement_items": 30,
                    "source_audit_candidates": 10,
                    "editorial_candidates": 40,
                    "critic_articles": 15,
                    "critic_newsletter_records": 5,
                },
            },
        )

        assert updated.status_code == 200
        assert updated.json()["profile"]["content_limits"] == {
            "lead_items": 2,
            "per_source": {
                "collections": 15,
                "foreign_media": 15,
                "gmail": 15,
                "markets": 15,
                "podcasts": 15,
                "reddit": 2,
                "web_search": 5,
                "youtube": 15,
            },
            "quality_floor": "strong",
            "target_items": 6,
            "total_items": 9,
        }
        assert updated.json()["profile"]["lookback_hours"] == 168
        assert updated.json()["profile"]["pipeline_limits"] == {
            "article_fetches": 90,
            "article_fetch_concurrency": 4,
            "model_refinement_items": 30,
            "source_audit_candidates": 10,
            "editorial_candidates": 40,
            "critic_articles": 15,
            "critic_newsletter_records": 5,
        }


def test_rebuild_preserves_topic_profile_lookback(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = database.upsert_topic_profile(
        {
            "statement": "Track memory stocks over three days",
            "scope": "Memory stock catalysts",
            "lookback_hours": 72,
            "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
        }
    )
    exploration = database.create_exploration(
        topic_id=topic["topic_id"],
        mode="show_now",
        source_selection=topic["profile"]["source_selection"],
        status="complete",
    )

    rebuilt = explore.start_rebuild(exploration["exploration_id"])

    assert rebuilt is not None
    assert rebuilt["status"] == "queued"
    assert rebuilt["progress"]["queue"]["action"] == "rebuild"
    assert rebuilt["progress"]["queue_options"]["lookback_hours"] == 72


def test_refined_rebuild_updates_same_topic_and_clears_session(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = database.upsert_topic_profile(
        {
            "statement": "Track memory stocks",
            "scope": "Memory stock catalysts",
            "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
        }
    )
    exploration = database.create_exploration(
        topic_id=topic["topic_id"],
        mode="show_now",
        source_selection=topic["profile"]["source_selection"],
        status="complete",
    )
    session = refinement.start_session(
        {
            "statement": topic["statement"],
            "topic_id": topic["topic_id"],
            "revisit": True,
            "source_selection": topic["profile"]["source_selection"],
        }
    )
    assert session["profile"]["topic_id"] == topic["topic_id"]
    assert session["status"] == "active"
    observed: dict[str, Any] = {}

    def fake_start_rebuild(exploration_id: str, **kwargs: Any) -> dict[str, Any]:
        observed["exploration_id"] = exploration_id
        observed.update(kwargs)
        return {
            **exploration,
            "status": "queued",
            "progress": {"queue": {"action": "rebuild"}},
        }

    monkeypatch.setattr(explore, "start_rebuild", fake_start_rebuild)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post(
            f"/api/explore/explorations/{exploration['exploration_id']}/rebuild",
            json={
                "source_selection": topic["profile"]["source_selection"],
                "lookback_hours": 96,
                "refinement_session_id": session["session_id"],
                "topic_profile": {
                    "topic_id": topic["topic_id"],
                    "statement": topic["statement"],
                    "scope": "Memory stock catalysts with supplier checks",
                    "lookback_hours": 96,
                    "source_selection": topic["profile"]["source_selection"],
                },
            },
        )

    assert response.status_code == 202
    assert observed["exploration_id"] == exploration["exploration_id"]
    assert observed["lookback_hours"] == 96
    saved = database.get_topic_profile(topic["topic_id"])
    assert saved is not None
    assert saved["profile"]["scope"] == "Memory stock catalysts with supplier checks"
    assert saved["profile"]["lookback_hours"] == 96
    assert database.get_refinement_session(session["session_id"]) is None


def test_scheduled_run_promotes_kept_sources(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = database.upsert_topic_profile(
        {
            "statement": "Explore local AI newsletters and communities",
            "scope": "Local AI ecosystem",
            "source_selection": {"gmail": True, "reddit": True, "podcasts": True, "web_search": True},
            "schedule": "daily",
        }
    )

    topic_profile = TopicProfile.from_dict(topic["profile"])
    gmail_payload = NormalizedPayload(
        id="gmail-1",
        source_type="gmail",
        source_name="Local AI Daily",
        raw_text="Newsletter roundup.",
        metadata={"sender_email": "newsletter@localai.example"},
    )
    reddit_payload = NormalizedPayload(
        id="reddit-1",
        source_type="reddit_thread",
        source_name="localai",
        raw_text="Thread signal.",
        metadata={"subreddit": "localAI"},
    )
    podcast_payload = NormalizedPayload(
        id="podcast-1",
        source_type="podcast_episode",
        source_name="Practical AI Podcast",
        raw_text="Episode notes.",
        metadata={"podcast_title": "Practical AI Podcast", "feed_url": "https://podcast.example.com/feed"},
    )
    web_payload = NormalizedPayload(
        id="search-1",
        source_type="gmail_link",
        source_name="Web Search Story",
        raw_text="Search results.",
        original_url="https://example.com/story",
        metadata={"search_query": "local AI ecosystem"},
    )

    discovery = DiscoveryResult(
        profile=topic_profile,
        candidates=(
            Candidate(adapter="gmail", payload=gmail_payload, score=0.99, reason="Mail"),
            Candidate(adapter="reddit", payload=reddit_payload, score=0.77, reason="Reddit"),
            Candidate(adapter="podcasts", payload=podcast_payload, score=0.65, reason="Podcasts"),
            Candidate(adapter="web_search", payload=web_payload, score=0.61, reason="Search"),
        ),
        statuses=(),
    )

    async def fake_discovery_run(self, *_args, **_kwargs) -> DiscoveryResult:
        return discovery

    async def fake_fetch_articles_for_payloads(payloads: list[NormalizedPayload], **_kwargs) -> list[ArticleFetchResult]:
        assert len(payloads) == 4
        return [
            ArticleFetchResult(
                payload=gmail_payload,
                original_url="https://mail.localai.example",
                final_url="https://mail.localai.example",
                title="Mailbox",
                text="g",
                excerpt="g",
                domain="mail.localai.example",
                status="fetched",
            ),
            ArticleFetchResult(
                payload=reddit_payload,
                original_url="https://reddit.com/r/localAI/1",
                final_url="https://reddit.com/r/localAI/1",
                title="Thread",
                text="r",
                excerpt="r",
                domain="reddit.com",
                status="fetched",
            ),
            ArticleFetchResult(
                payload=podcast_payload,
                original_url="https://podcast.example.com/episode/1",
                final_url="https://podcast.example.com/episode/1",
                title="Episode",
                text="p",
                excerpt="p",
                domain="podcast.example.com",
                status="fetched",
            ),
            ArticleFetchResult(
                payload=web_payload,
                original_url="https://example.com/story",
                final_url="https://example.com/story",
                title="Story",
                text="w",
                excerpt="w",
                domain="example.com",
                status="dropped",
            ),
        ]

    async def fake_run_digest_core(
        *,
            profile: TopicProfile,
            payloads: list[NormalizedPayload],
            fetched_articles: list[ArticleFetchResult],
            lookback_hours: int,
            inference_run_id: str,
            progress: dict[str, object],
            persist: object,
    ) -> list[ArticleFetchResult]:
        return fetched_articles

    monkeypatch.setattr(DiscoveryRunner, "run", fake_discovery_run)
    monkeypatch.setattr(explore, "_run_digest_core", fake_run_digest_core)
    monkeypatch.setattr(explore, "fetch_articles_for_payloads", fake_fetch_articles_for_payloads)
    monkeypatch.setattr(
        database,
        "render_ingested_issue",
        lambda *_args, **_kwargs: "<html><body>explore result</body></html>",
    )

    result = asyncio.run(
        explore.run_scheduled(
            topic["topic_id"],
            source_selection={"gmail": True, "reddit": True, "podcasts": True, "web_search": True},
        )
    )
    assert result is not None
    exploration = result["exploration"]
    assert exploration["status"] == "complete"
    stage_seconds = result["brief"]["stats"]["stage_seconds"]
    assert stage_seconds["editorial"] > 0
    assert stage_seconds["publishing"] > 0
    strategy = result["brief"]["stats"]["search_strategy"]
    assert "local ai ecosystem" in strategy["summary"]
    assert "web search" in strategy["summary"]

    promoted_sources = database.list_promoted_sources(topic["topic_id"])
    assert len(promoted_sources) == 3
    promoted_keys = {
        (source["adapter"], source["ref"], source["has_feed"], source["feed_url"]) for source in promoted_sources
    }
    assert ("gmail", "newsletter@localai.example", False, None) in promoted_keys
    assert ("reddit", "r/localAI", False, None) in promoted_keys
    assert ("podcasts", "Practical AI Podcast", True, "https://podcast.example.com/feed") in promoted_keys

    topic_profile_after = database.get_topic_profile(topic["topic_id"]) or {}
    assert len(topic_profile_after["profile"].get("promoted_sources", [])) == 3


def test_scheduled_run_promotes_deduped_sources(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = database.upsert_topic_profile(
        {
            "statement": "Explore duplicated sources",
            "scope": "Duplicate source handling",
            "source_selection": {"gmail": True},
            "schedule": "daily",
        }
    )

    topic_profile = TopicProfile.from_dict(topic["profile"])
    gmail_payload = NormalizedPayload(
        id="gmail-1",
        source_type="gmail",
        source_name="Local AI Daily",
        raw_text="Newsletter roundup.",
        metadata={"sender_email": "newsletter@localai.example"},
    )

    discovery = DiscoveryResult(
        profile=topic_profile,
        candidates=(
            Candidate(adapter="gmail", payload=gmail_payload, score=0.99, reason="Mail"),
        ),
        statuses=(),
    )

    async def fake_discovery_run(self, *_args, **_kwargs) -> DiscoveryResult:
        return discovery

    async def fake_fetch_articles_for_payloads(_payloads: list[NormalizedPayload], **_kwargs) -> list[ArticleFetchResult]:
        return [
            ArticleFetchResult(
                payload=gmail_payload,
                original_url="https://mail.localai.example/1",
                final_url="https://mail.localai.example/1",
                title="Mailbox 1",
                text="g",
                excerpt="g",
                domain="mail.localai.example",
                status="fetched",
            ),
            ArticleFetchResult(
                payload=gmail_payload,
                original_url="https://mail.localai.example/2",
                final_url="https://mail.localai.example/2",
                title="Mailbox 2",
                text="g",
                excerpt="g",
                domain="mail.localai.example",
                status="fetched",
            ),
        ]

    async def fake_run_digest_core(
        *,
            profile: TopicProfile,
            payloads: list[NormalizedPayload],
            fetched_articles: list[ArticleFetchResult],
            lookback_hours: int,
            inference_run_id: str,
            progress: dict[str, object],
            persist: object,
    ) -> list[ArticleFetchResult]:
        return fetched_articles

    monkeypatch.setattr(DiscoveryRunner, "run", fake_discovery_run)
    monkeypatch.setattr(explore, "_run_digest_core", fake_run_digest_core)
    monkeypatch.setattr(explore, "fetch_articles_for_payloads", fake_fetch_articles_for_payloads)
    monkeypatch.setattr(
        database,
        "render_ingested_issue",
        lambda *_args, **_kwargs: "<html><body>explore result</body></html>",
    )

    result = asyncio.run(
        explore.run_scheduled(
            topic["topic_id"],
            source_selection={"gmail": True},
        )
    )
    assert result is not None
    assert result["exploration"]["status"] == "complete"

    promoted_sources = database.list_promoted_sources(topic["topic_id"])
    assert len(promoted_sources) == 1
    assert promoted_sources[0]["adapter"] == "gmail"
    assert promoted_sources[0]["ref"] == "newsletter@localai.example"


def test_discovery_runner_dedupes_and_marks_opt_outs() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "AI agents for local infrastructure",
            "scope": "Practical agent workflows",
            "source_selection": {"gmail": True, "reddit": False},
        }
    )
    registry = SourceRegistry(
        [
            FakeAdapter(
                "gmail",
                [
                    candidate("gmail", "https://example.com/story?utm_source=x", 0.7),
                    candidate("gmail", "https://example.com/story", 0.6),
                ],
            ),
            FakeAdapter("reddit", [candidate("reddit", "https://reddit.com/r/test/1", 0.9)]),
        ]
    )

    result = asyncio.run(
        DiscoveryRunner(registry).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-1", candidate_limit=10),
        )
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].score == 0.7
    statuses = {status.name: status.status for status in result.statuses}
    assert statuses["gmail"] == "completed"
    assert statuses["reddit"] == "skipped"


def test_discovery_runner_applies_per_source_content_limits() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "AI agents for local infrastructure",
            "scope": "Practical agent workflows",
            "source_selection": {"gmail": True, "web_search": True},
            "content_limits": {"per_source": {"gmail": 1, "web_search": 2}},
        }
    )
    registry = SourceRegistry(
        [
            FakeAdapter(
                "gmail",
                [
                    candidate("gmail", "https://example.com/mail-1", 0.9),
                    candidate("gmail", "https://example.com/mail-2", 0.8),
                ],
            ),
            FakeAdapter(
                "web_search",
                [
                    candidate("web_search", "https://example.com/web-1", 0.7),
                    candidate("web_search", "https://example.com/web-2", 0.6),
                    candidate("web_search", "https://example.com/web-3", 0.5),
                ],
            ),
        ]
    )

    result = asyncio.run(
        DiscoveryRunner(registry).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-1", candidate_limit=10),
        )
    )

    assert [candidate.adapter for candidate in result.candidates] == ["gmail", "web_search", "web_search"]
    assert [candidate.payload.original_url for candidate in result.candidates] == [
        "https://example.com/mail-1",
        "https://example.com/web-1",
        "https://example.com/web-2",
    ]


def test_discovery_runner_backfills_unused_source_limit_capacity() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "AI agents for local infrastructure",
            "scope": "Practical agent workflows",
            "source_selection": {"gmail": True, "web_search": True},
            "content_limits": {"total_items": 4, "per_source": {"gmail": 2, "web_search": 1}},
        }
    )
    registry = SourceRegistry(
        [
            FakeAdapter("gmail", []),
            FakeAdapter(
                "web_search",
                [
                    candidate("web_search", "https://example.com/web-1", 0.9),
                    candidate("web_search", "https://example.com/web-2", 0.8),
                    candidate("web_search", "https://example.com/web-3", 0.7),
                    candidate("web_search", "https://example.com/web-4", 0.6),
                ],
            ),
        ]
    )

    result = asyncio.run(
        DiscoveryRunner(registry).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-1", candidate_limit=4),
        )
    )

    assert [candidate.payload.original_url for candidate in result.candidates] == [
        "https://example.com/web-1",
        "https://example.com/web-2",
        "https://example.com/web-3",
        "https://example.com/web-4",
    ]


def test_discovery_runner_applies_exclusions() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "AI agents and tooling",
            "scope": "Practical updates",
            "exclusions": ["vapor", "noise"],
        }
    )
    excluded = Candidate(
        adapter="web_search",
        payload=NormalizedPayload(
            source_type="web_search_item",
            source_name="web_search",
            raw_text="This is vaporware gossip about a side topic.",
            original_url="https://example.com/vapor",
            id="excluded",
        ),
        score=0.9,
    )
    included = Candidate(
        adapter="web_search",
        payload=NormalizedPayload(
            source_type="web_search_item",
            source_name="web_search",
            raw_text="This is practical infrastructure signal with real details.",
            original_url="https://example.com/insight",
            id="included",
        ),
        score=0.8,
    )
    excluded_by_url = Candidate(
        adapter="web_search",
        payload=NormalizedPayload(
            source_type="web_search_item",
            source_name="web_search",
            raw_text="Noisy but relevant signal.",
            original_url="https://example.com/noise",
            metadata={"tags": ["noise", "signal"]},
            id="excluded-by-url",
        ),
        score=0.75,
    )
    registry = SourceRegistry([FakeAdapter("web_search", [included, excluded, excluded_by_url], timeout_seconds=1.0)])

    result = asyncio.run(
        DiscoveryRunner(registry).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-4", candidate_limit=10),
        )
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].payload.original_url == "https://example.com/insight"
    assert len(result.exclusions) == 2
    adapters = {entry["adapter"] for entry in result.exclusions}
    assert "web_search" in adapters
    excluded_term_sets = [set(entry.get("excluded_by") or []) for entry in result.exclusions]
    assert any("vapor" in terms for terms in excluded_term_sets)
    assert any("noise" in terms for terms in excluded_term_sets)


def test_discovery_runner_does_not_match_exclusions_against_search_query() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "Mexico City travel",
            "scope": "CDMX food museums and bike rides",
            "exclusions": ["glbq issues or advice"],
        }
    )
    relevant = Candidate(
        adapter="web_search",
        payload=NormalizedPayload(
            source_type="web_search_item",
            source_name="Mexico City museum and food guide",
            raw_text="A practical CDMX guide for museums, food, and bike rides.",
            original_url="https://example.com/cdmx",
            metadata={"search_query": "Mexico City travel Avoid: glbq issues or advice"},
            id="cdmx",
        ),
        score=0.85,
    )

    result = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("web_search", [relevant], timeout_seconds=1.0)])).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-exclusion-query", candidate_limit=10),
        )
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].payload.id == "cdmx"
    assert result.exclusions == ()


def test_discovery_runner_filters_cross_topic_source_bleed_when_topic_matches_exist() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "Mexico City travel",
            "scope": "CDMX food museums and bike rides",
        }
    )
    cdmx = Candidate(
        adapter="web_search",
        payload=NormalizedPayload(
            source_type="web_search_item",
            source_name="Mexico City food and museum guide",
            raw_text="CDMX museums, food halls, bike rides, and neighborhood walks.",
            original_url="https://example.com/cdmx",
            id="cdmx",
        ),
        score=0.71,
    )
    ai = Candidate(
        adapter="gmail",
        payload=NormalizedPayload(
            source_type="gmail",
            source_name="AI newsletter",
            raw_text="Cursor Composer, coding agents, and local model infrastructure.",
            original_url="https://example.com/ai",
            id="ai",
        ),
        score=0.95,
    )

    result = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("web_search", [cdmx]), FakeAdapter("gmail", [ai])])).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-source-bleed", candidate_limit=10),
        )
    )

    assert [candidate.payload.id for candidate in result.candidates] == ["cdmx"]
    assert any(entry["candidate_id"] == "ai" and "low_topic_overlap" in entry["excluded_by"] for entry in result.exclusions)


def test_discovery_runner_drops_cross_topic_source_bleed_when_no_topic_matches() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "Mexico City travel in August 2026",
            "scope": "CDMX food museums bike rides and neighborhood walks",
        }
    )
    ai = Candidate(
        adapter="gmail",
        payload=NormalizedPayload(
            source_type="gmail",
            source_name="AI newsletter",
            raw_text="Long-running coding agents, model infrastructure, and autonomous workflow launches.",
            original_url="https://example.com/ai",
            id="ai",
        ),
        score=0.95,
    )

    result = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("gmail", [ai])])).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-no-topic-match", candidate_limit=10),
        )
    )

    assert result.candidates == ()
    assert any(entry["candidate_id"] == "ai" and "low_topic_overlap" in entry["excluded_by"] for entry in result.exclusions)


def test_explore_progress_includes_exclusion_reasons(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = database.upsert_topic_profile(
        {
            "statement": "Explore local AI operations",
            "scope": "AI operations signal",
            "exclusions": ["noise", "rumor"],
            "source_selection": {"web_search": True},
        }
    )

    profile = TopicProfile.from_dict(topic["profile"])
    included_payload = NormalizedPayload(
        source_type="web_search_item",
        source_name="web_search",
        raw_text="Practical AI operations update.",
        original_url="https://example.com/insight",
        id="included-item",
    )
    excluded_payload = NormalizedPayload(
        source_type="web_search_item",
        source_name="web_search",
        raw_text="Noisy rumor signal.",
        original_url="https://example.com/noise",
        id="excluded-item",
    )

    discovery = DiscoveryResult(
        profile=profile,
        candidates=(
            Candidate(adapter="web_search", payload=included_payload, score=0.88),
        ),
        statuses=(
            AdapterStatus(
                name="web_search",
                status="completed",
                candidate_count=1,
                message="ok",
                timeout_seconds=10.0,
            ),
        ),
        exclusions=(
            {
                "adapter": "web_search",
                "candidate_id": "excluded-item",
                "original_url": excluded_payload.original_url,
                "source_type": excluded_payload.source_type,
                "source_name": excluded_payload.source_name,
                "title": "Noisy rumor signal.",
                "excluded_by": ["noise", "rumor"],
                "reason": "Filtered by exclusions: noise, rumor",
            },
        ),
    )

    async def fake_discovery_run(self, *_args, **_kwargs) -> DiscoveryResult:
        return discovery

    async def fake_fetch_articles_for_payloads(payloads: list[NormalizedPayload], **_kwargs) -> list[ArticleFetchResult]:
        return []

    async def fake_run_digest_core(
        *,
            profile: TopicProfile,
            payloads: list[NormalizedPayload],
            fetched_articles: list[ArticleFetchResult],
            lookback_hours: int,
            inference_run_id: str,
            progress: dict[str, Any],
            persist: object,
    ) -> list[ArticleFetchResult]:
        return []

    monkeypatch.setattr(DiscoveryRunner, "run", fake_discovery_run)
    monkeypatch.setattr(explore, "fetch_articles_for_payloads", fake_fetch_articles_for_payloads)
    monkeypatch.setattr(explore, "_run_digest_core", fake_run_digest_core)
    monkeypatch.setattr(
        database,
        "render_ingested_issue",
        lambda *_args, **_kwargs: "<html><body>explore result</body></html>",
    )

    result = asyncio.run(
        explore.run_scheduled(
            topic["topic_id"],
            source_selection={"web_search": True},
        )
    )
    assert result is not None
    assert result["exploration"]["status"] == "complete"
    assert result["discovery"].get("exclusions")
    assert result["exploration"]["progress"]["exclusions"]
    assert result["exploration"]["progress"]["exclusions"][0]["candidate_id"] == "excluded-item"
    assert "noise" in result["exploration"]["progress"]["exclusions"][0]["excluded_by"]


def test_discovery_runner_times_out_slow_adapter() -> None:
    profile = TopicProfile.from_dict({"statement": "AI agents", "scope": "AI agents"})
    registry = SourceRegistry([SlowAdapter("podcasts", [candidate("podcasts", "https://example.com/pod", 0.8)], timeout_seconds=0.01)])

    result = asyncio.run(
        DiscoveryRunner(registry).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-2", candidate_limit=10),
        )
    )

    assert result.candidates == ()
    assert result.statuses[0].status == "timed_out"


def test_discovery_runner_applies_good_for_ranking_weights() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "Breaking latest headlines and fresh AI updates",
            "scope": "AI news sweep for product teams",
            "depth": "informed-generalist",
            "recency_weighting": "breaking",
        }
    )
    fresh_adapter = FakeAdapter(
        "web_search",
        [candidate("web_search", "https://example.com/fresh", 0.59)],
    )
    fresh_adapter.good_for = ("breaking_news", "fresh_sources")
    steady_adapter = FakeAdapter(
        "reddit",
        [candidate("reddit", "https://example.com/steady", 0.62)],
    )
    steady_adapter.good_for = ("deep_context",)

    result = asyncio.run(
        DiscoveryRunner(SourceRegistry([fresh_adapter, steady_adapter])).run(
            profile,
            context=SourceAdapterContext(exploration_id="explore-3", candidate_limit=10),
        )
    )

    assert len(result.candidates) == 2
    assert result.candidates[0].payload.original_url == "https://example.com/fresh"
    assert result.candidates[0].score > result.candidates[1].score


def test_explore_api_creates_topic_profile(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore local AI infrastructure",
                "scope": "Apple Silicon inference tools",
                "depth": "practitioner",
                "recency_weighting": "balanced",
                "exclusions": [],
                "source_selection": {"gmail": True, "reddit": False},
            },
        )

        discovery = client.post(
            f"/api/explore/topic-profiles/{response.json()['topic_id']}/discover",
            json={"source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": True}},
        )
        run = client.post(
            f"/api/explore/topic-profiles/{response.json()['topic_id']}/run",
            json={"source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False}},
        )
        run_exploration = run.json()
        exploration_id = run_exploration["exploration"]["exploration_id"]
        poll = client.get(f"/api/explore/explorations/{exploration_id}")
        for _ in range(60):
            if poll.status_code != 200:
                break
            poll_body = poll.json()
            if poll_body["status"] not in {"queued", "running"}:
                break
            time.sleep(0.1)
            poll = client.get(f"/api/explore/explorations/{exploration_id}")

    assert response.status_code == 201
    body = response.json()
    assert body["statement"] == "Explore local AI infrastructure"
    assert body["profile"]["source_selection"]["reddit"] is False
    assert discovery.status_code == 202
    discovery_body = discovery.json()
    assert discovery_body["exploration"]["status"] == "complete"
    assert discovery_body["discovery"]["candidate_count"] == 0
    statuses = {status["name"]: status["status"] for status in discovery_body["discovery"]["statuses"]}
    assert statuses["web_search"] == "skipped"
    assert statuses["gmail"] == "skipped"
    assert run.status_code == 202
    assert poll.status_code == 200
    poll_body = poll.json()
    assert poll_body["status"] in {"complete", "failed"}
    assert poll_body["status"] == "complete"
    assert poll_body["brief_ref"]
    assert poll_body["progress"]["brief"]["html_path"].endswith("/brief/html")
    run_body = poll_body

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        brief = client.get(run_body["progress"]["brief"]["html_path"])

    assert brief.status_code == 200
    assert "No source content was found for this brief." in brief.text

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        email = client.post(
            f"/api/explore/explorations/{run_body['exploration_id']}/email",
            json={},
        )

    assert email.status_code == 200
    assert email.json()["status"] == "skipped"
    assert email.json()["error"] == "No delivery email configured."


def test_explore_email_marks_exploration_emailed(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    class FakeSend:
        def execute(self) -> dict[str, str]:
            return {"id": "fake-message-id"}

    class FakeMessages:
        def send(self, **_kwargs) -> FakeSend:
            return FakeSend()

    class FakeUsers:
        def messages(self) -> FakeMessages:
            return FakeMessages()

    class FakeService:
        def users(self) -> FakeUsers:
            return FakeUsers()

    monkeypatch.setattr(email_delivery, "_gmail_service", lambda: FakeService())

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        profile = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore local AI infrastructure",
                "scope": "Apple Silicon inference tools",
                "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
            },
        ).json()
        run = client.post(
            f"/api/explore/topic-profiles/{profile['topic_id']}/run",
            json={"source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False}},
        ).json()
        sent = client.post(
            f"/api/explore/explorations/{run['exploration']['exploration_id']}/email",
            json={"recipient_email": "adrian@example.com"},
        )
        exploration = client.get(f"/api/explore/explorations/{run['exploration']['exploration_id']}")

    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"
    assert sent.json()["message_id"] == "fake-message-id"
    assert exploration.json()["emailed"] is True


def test_refinement_session_finalizes_topic_profile(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={
                "statement": "Explore local AI agents",
                "source_selection": {"gmail": False, "reddit": False},
            },
        )
        assert started.status_code == 201
        session_id = started.json()["session_id"]
        assert started.json()["pending_field"] == "scope"
        assert started.json()["messages"][0]["role"] == "assistant"

        answered_scope = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "Small team deployment patterns and practical tools"},
        )
        assert answered_scope.status_code == 200
        assert answered_scope.json()["pending_field"] == "related_interests"

        answered_related = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "MCP servers and local inference"},
        )
        assert answered_related.status_code == 200
        assert answered_related.json()["pending_field"] == "depth"

        answered_depth = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "balanced"},
        )
        assert answered_depth.status_code == 200
        assert answered_depth.json()["pending_field"] == "recency_weighting"

        answered_recency = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "balanced"},
        )
        assert answered_recency.status_code == 200
        assert answered_recency.json()["pending_field"] == "requested_sources"

        answered_sources = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "include the podcast: The Daily AI Brief"},
        )
        assert answered_sources.status_code == 200
        assert answered_sources.json()["pending_field"] == "exclusions"

        finalized = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "consumer chatbot rumors", "just_go_now": False},
        )

    assert finalized.status_code == 200
    body = finalized.json()
    assert body["status"] == "finalized"
    assert body["topic_id"]
    assert body["topic_profile"]["profile"]["scope"] == "Small team deployment patterns and practical tools"
    assert body["topic_profile"]["profile"]["exclusions"] == ["consumer chatbot rumors"]
    assert body["topic_profile"]["profile"]["requested_sources"] == [
        {"adapter": "podcasts", "ref": "The Daily AI Brief"}
    ]


def test_refinement_session_default_sources_are_web_only(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={"statement": "Explore local AI agents"},
        )

    assert started.status_code == 201
    assert started.json()["profile"]["source_selection"] == {
        "gmail": False,
        "reddit": False,
            "podcasts": False,
            "web_search": True,
            "foreign_media": False,
            "youtube": False,
        "collections": False,
        "markets": False,
    }


def test_refinement_agent_requires_two_answers_before_finalizing(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    class _DefensiblePlanModelClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            return {
                "profile_patch": {
                    "scope": "Local AI deployment patterns",
                    "subtopics": ["Apple Silicon runtimes", "team deployment workflows"],
                    "keywords": ["local AI", "deployment", "Apple Silicon"],
                    "search_queries": ["local AI deployment patterns"],
                    "source_queries": {"web_search": ["Apple Silicon local AI deployment patterns"]},
                    "depth": "practitioner",
                    "recency_weighting": "recent",
                },
                "ready_to_build": True,
                "next_question": None,
                "reasoning_summary": "The request is specific enough to build a practical monitoring plan.",
            }

    model_client = _DefensiblePlanModelClient()
    monkeypatch.setattr(
        "backend.app.services.refinement.ModelClient.from_settings",
        lambda *_args, **_kwargs: model_client,
    )

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={"statement": "Explore local AI deployment"},
        )
        session_id = started.json()["session_id"]
        first = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "Make it practical for product teams"},
        )
        second = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "Focus on recent material"},
        )

    assert started.status_code == 201
    assert first.status_code == 200
    assert second.status_code == 200
    assert started.json()["status"] == "active"
    assert first.json()["status"] == "active"
    body = second.json()
    assert body["status"] == "finalized"
    assert body["topic_profile"]["profile"]["scope"] == "Local AI deployment patterns"
    assert body["topic_profile"]["profile"]["depth"] == "practitioner"
    assert body["topic_profile"]["profile"]["recency_weighting"] == "recent"
    prompt_payload = model_client.calls[0]["prompt"]
    assert isinstance(prompt_payload, str)
    assert '"min_turns": 2' in prompt_payload
    assert "Ask at least min_turns meaningful refinement questions" in prompt_payload


def test_refinement_agent_does_not_reask_stated_market_recency_and_exclusions(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    class _RedundantQuestionModelClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            return {
                "profile_patch": {
                    "scope": "Track Micron, SK Hynix, Kioxia, and SanDisk performance through recent memory-market news.",
                    "keywords": ["Micron", "SK Hynix", "Kioxia", "SanDisk", "memory stocks"],
                    "search_queries": ["Micron Hynix Kioxia SanDisk memory news previous 3 days"],
                    "source_queries": {"web_search": ["Micron Hynix Kioxia SanDisk news previous 3 days -MSN -Yahoo"]},
                    "recency_weighting": "breaking",
                    "exclusions": ["MSN", "Yahoo News"],
                },
                "ready_to_build": False,
                "next_question": "How recent should I look, and is there anything else to avoid?",
                "reasoning_summary": "The user gave companies, recency, and excluded aggregators.",
            }

    model_client = _RedundantQuestionModelClient()
    monkeypatch.setattr(
        "backend.app.services.refinement.ModelClient.from_settings",
        lambda *_args, **_kwargs: model_client,
    )

    statement = (
        "An an investor I'm interested in these company's performance Micron, Hynix, Kioxia, Sandisk. "
        "Track news related to them; focus primarily on news coming from sites that are not like MSN or Yahoo news. "
        "Limit lookback to news coming in previous 3 days."
    )
    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={"statement": statement, "source_selection": {"web_search": True, "markets": True}},
        )

    assert started.status_code == 201
    body = started.json()
    question = body["messages"][0]["content"].lower()
    assert "how recent" not in question
    assert "avoid" not in question
    assert "signals" in question or "actionable" in question or "catalysts" in question
    assert body["profile"]["recency_weighting"] == "recent"
    assert body["profile"]["lookback_hours"] == 72
    assert body["profile"]["source_scope_answered"] is True
    assert "MSN" in body["profile"]["exclusions"]
    assert "Yahoo News" in body["profile"]["exclusions"]
    prompt_payload = model_client.calls[0]["prompt"]
    assert isinstance(prompt_payload, str)
    assert "already_inferred" in prompt_payload
    assert "previous 3 days" in prompt_payload


def test_refinement_agent_does_not_repeat_same_strategy_question(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    repeated_question = (
        "What would make this brief actionable for you: catalysts, risks, valuation context, "
        "or company-by-company comparisons?"
    )

    class _RepeatingQuestionModelClient:
        async def complete_json(self, **_kwargs: object) -> dict[str, object]:
            return {
                "profile_patch": {
                    "scope": "AI memory supply-chain investment signals",
                    "keywords": ["Micron", "SK Hynix", "Kioxia", "HBM"],
                    "search_queries": ["AI memory supply chain investment signals"],
                    "source_queries": {"web_search": ["Micron SK Hynix Kioxia HBM catalysts"]},
                    "depth": "practitioner",
                    "recency_weighting": "recent",
                },
                "ready_to_build": False,
                "next_question": repeated_question,
                "reasoning_summary": "The plan needs decision criteria.",
            }

    monkeypatch.setattr(
        "backend.app.services.refinement.ModelClient.from_settings",
        lambda *_args, **_kwargs: _RepeatingQuestionModelClient(),
    )

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={
                "statement": "Find investable signals for AI memory picks and shovels.",
                "source_selection": {"web_search": True, "markets": True},
            },
        )
        session_id = started.json()["session_id"]
        updated = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "catalysts, risks, and company-by-company comparisons"},
        )

    assert started.status_code == 201
    assert updated.status_code == 200
    assistant_questions = [
        message["content"]
        for message in updated.json()["messages"]
        if message["role"] == "assistant" and message["content"].endswith("?")
    ]
    assert assistant_questions[0] == repeated_question
    assert assistant_questions[-1] != repeated_question
    assert len(set(assistant_questions)) == len(assistant_questions)


def test_refinement_session_uses_refinement_model(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    class _RefinementModelClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            ready = len(self.calls) >= 6
            return {
                "profile_patch": {
                    "scope": "Small team infrastructure workflows",
                    "subtopics": ["MCP deployment", "team workflows"],
                    "keywords": ["local AI", "MCP", "deployment"],
                    "search_queries": ["small team local AI deployment workflows MCP"],
                    "source_queries": {
                        "web_search": ["MCP local AI deployment playbooks"],
                        "reddit": ["local AI MCP deployment teams"],
                    },
                    "requested_sources": [{"adapter": "youtube", "ref": "youtube"}],
                    "source_selection": {"youtube": True},
                    "depth": "practitioner",
                    "recency_weighting": "all_available",
                    "exclusions": ["consumer chatter", "rumor"] if ready else [],
                },
                "ready_to_build": ready,
                "next_question": None if ready else "Which deployment constraint matters most?",
                "reasoning_summary": "Built a source-aware retrieval plan.",
            }

    model_client = _RefinementModelClient()
    monkeypatch.setattr(
        "backend.app.services.refinement.ModelClient.from_settings",
        lambda *_args, **_kwargs: model_client,
    )

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
                json={
                    "statement": "Explore practical local AI updates",
                    "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": True},
                    "models": {"refinement": "conversation-model"},
                },
        )
        session_id = started.json()["session_id"]
        scope = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "I want deployment workflows for teams", "models": {"refinement": "conversation-model"}},
        )
        related = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "include MCP deployment", "models": {"refinement": "conversation-model"}},
        )
        depth = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "go deep on practice", "models": {"refinement": "conversation-model"}},
        )
        recency = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "as much as possible", "models": {"refinement": "conversation-model"}},
        )
        requested = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "no specific sources", "models": {"refinement": "conversation-model"}},
        )
        finalized = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "drop anything about hype", "models": {"refinement": "conversation-model"}},
        )

    assert scope.status_code == 200
    assert related.status_code == 200
    assert depth.status_code == 200
    assert recency.status_code == 200
    assert requested.status_code == 200
    assert finalized.status_code == 200
    body = finalized.json()
    assert body["profile"]["scope"] == "Small team infrastructure workflows"
    assert body["profile"]["depth"] == "practitioner"
    assert body["profile"]["recency_weighting"] == "all_available"
    assert body["profile"]["exclusions"] == ["consumer chatter", "rumor"]
    assert body["profile"]["search_queries"] == ["small team local AI deployment workflows MCP"]
    assert body["profile"]["source_queries"]["web_search"] == ["MCP local AI deployment playbooks"]
    assert "reddit" not in body["profile"]["source_queries"]
    assert body["profile"]["requested_sources"] == []
    assert body["profile"]["source_selection"]["youtube"] is False
    assert body["topic_profile"]["profile"]["scope"] == "Small team infrastructure workflows"
    assert body["topic_profile"]["profile"]["depth"] == "practitioner"
    assert body["topic_profile"]["profile"]["recency_weighting"] == "all_available"
    assert body["topic_profile"]["profile"]["models"]["refinement"] == "conversation-model"
    assert len(model_client.calls) >= 3


def test_refinement_model_client_marks_private_sources_for_local_routing(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    calls: list[dict[str, Any]] = []
    marker_client = object()

    def fake_client_for_agent(agent: str, **kwargs: Any) -> Any:
        calls.append({"agent": agent, **kwargs})
        return type("Resolution", (), {"client": marker_client})()

    monkeypatch.setattr(refinement.model_routing, "client_for_agent", fake_client_for_agent)

    client = refinement._refinement_model_client(
        {
            "models": {"refinement": None},
            "source_selection": {"gmail": True, "collections": True, "web_search": True},
        }
    )

    assert client is marker_client
    assert calls[0]["agent"] == "refinement"
    assert calls[0]["items"] == [{"source_type": "gmail"}, {"source_type": "collection_chunk"}]


def test_refinement_session_accepts_no_exclusions(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={"statement": "Track useful AI product strategy"},
        )
        session_id = started.json()["session_id"]
        client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "Practical product strategy moves for small teams"},
        )
        client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "none"},
        )
        client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "balanced"},
        )
        client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "balanced"},
        )
        client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "none"},
        )
        finalized = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "none"},
        )

    assert finalized.status_code == 200
    body = finalized.json()
    assert body["status"] == "finalized"
    assert body["topic_profile"]["profile"]["exclusions"] == []


def test_refinement_session_accepts_model_override(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={
                "statement": "Explore model routing behavior",
                "source_selection": {"gmail": True},
                "models": {"brief": "route-model"},
            },
        )
        assert started.status_code == 201
        session_id = started.json()["session_id"]
        assert started.json()["profile"]["models"]["brief"] == "route-model"

        finalized = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={
                "answer": "Practical model routing changes",
                "just_go_now": True,
                "models": {"brief": "route-model"},
            },
        )

    assert finalized.status_code == 200
    body = finalized.json()
    assert body["profile"]["models"]["brief"] == "route-model"
    assert body["topic_profile"]["profile"]["models"]["brief"] == "route-model"


def test_refinement_session_ignores_invalid_model_payload_values(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    session = refinement.start_session(
        {
            "statement": "Explore malformed model payloads",
            "models": {
                "brief": 123,  # type: ignore[arg-type]
                "refinement": "   ",
                "unexpected": "ignored",
            },
        }
    )
    assert session["profile"]["models"]["brief"] is None
    assert session["profile"]["models"]["refinement"] is None


def test_refinement_session_api_ignores_invalid_model_payload_values(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={
                "statement": "Explore endpoint model sanitization",
                "models": {
                    "brief": 123,
                    "refinement": None,
                    "unexpected": {"kind": "ignored"},
                },
            },
        )
        assert started.status_code == 201

        advance = client.post(
            f"/api/explore/refinement-sessions/{started.json()['session_id']}/messages",
            json={
                "just_go_now": True,
                "models": {
                    "brief": ["invalid", "list"],
                    "refinement": {"bad": "model"},
                },
            },
        )

    assert advance.status_code == 200
    started_body = started.json()
    assert started_body["profile"]["models"]["brief"] is None
    body = advance.json()
    assert body["profile"]["models"]["brief"] is None
    assert body["profile"]["models"]["refinement"] is None
    assert body["topic_profile"]["profile"]["models"]["brief"] is None
    assert body["topic_profile"]["profile"]["models"]["refinement"] is None


def test_gmail_refinement_discovers_and_confirms_newsletter_rules(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    async def fake_discover_newsletter_candidates(**_kwargs: Any) -> list[Any]:
        class CandidateRecord:
            sender = "ai@example.com"
            sender_name = "AI Weekly"
            subject = "AI Weekly: agents and infrastructure"
            message_count = 2
            latest_at = "2026-05-28T12:00:00+00:00"

            def to_dict(self) -> dict[str, Any]:
                return {
                    "sender": self.sender,
                    "sender_name": self.sender_name,
                    "subject": self.subject,
                    "message_count": self.message_count,
                    "latest_at": self.latest_at,
                }

        return [CandidateRecord()]

    monkeypatch.setattr(refinement, "discover_newsletter_candidates", fake_discover_newsletter_candidates)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={
                "statement": "Track AI infrastructure",
                "source_selection": {"gmail": True, "web_search": True},
            },
        )
        assert started.status_code == 201
        assert started.json()["pending_field"] == "gmail_rules"
        assert "How do you want me to use Gmail" in started.json()["messages"][0]["content"]

        searched = client.post(
            f"/api/explore/refinement-sessions/{started.json()['session_id']}/messages",
            json={"answer": "AI related newsletters received in last 7 days"},
        )
        assert searched.status_code == 200
        body = searched.json()
        assert body["pending_field"] == "gmail_sender_selection"
        assert body["profile"]["gmail_rules"]["lookback_hours"] == 168
        assert body["profile"]["gmail_rules"]["candidates"][0]["sender"] == "ai@example.com"
        assert "AI Weekly" in body["messages"][-1]["content"]

        confirmed = client.post(
            f"/api/explore/refinement-sessions/{started.json()['session_id']}/messages",
            json={"answer": "1"},
        )
        assert confirmed.status_code == 200
        confirmed_body = confirmed.json()
        # Gmail is one step of the interview, not the whole thing: after approving a
        # sender the session must keep interviewing instead of finalizing.
        assert confirmed_body["status"] == "active"
        assert confirmed_body["profile"]["gmail_rules"]["include_senders"] == ["ai@example.com"]
        assert {"adapter": "gmail", "ref": "ai@example.com"} in confirmed_body["profile"]["requested_sources"]
        assert "Approved ai@example.com" in confirmed_body["messages"][-2]["content"]
        # A real follow-up question is asked rather than ending the conversation.
        assert confirmed_body["pending_field"] not in (None, "gmail_rules", "gmail_sender_selection")
        assert confirmed_body["messages"][-1]["role"] == "assistant"

        finalized = client.post(
            f"/api/explore/refinement-sessions/{started.json()['session_id']}/messages",
            json={"just_go_now": True},
        )
        assert finalized.status_code == 200
        final_body = finalized.json()
        assert final_body["status"] == "finalized"
        assert final_body["profile"]["gmail_rules"]["include_senders"] == ["ai@example.com"]
        assert {"adapter": "gmail", "ref": "ai@example.com"} in final_body["profile"]["requested_sources"]


def test_create_topic_profile_accepts_model_override(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore model routing payload",
                "scope": "Model routing checks",
                "models": {"brief": "create-route", "refinement": "conversation-route"},
            },
        )
    assert created.status_code == 201
    body = created.json()
    assert body["profile"]["models"]["brief"] == "create-route"
    assert body["profile"]["models"]["refinement"] == "conversation-route"


def test_create_topic_profile_rejects_unsupported_schedule(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore unsupported schedules",
                "scope": "Schedule validation",
                "schedule": "quarter-hourly",
            },
        )

    assert created.status_code == 422


def test_save_topic_profile_rejects_unsupported_schedule(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    with pytest.raises(ValueError, match="Unsupported topic profile schedule"):
        explore.save_topic_profile(
            {
                "statement": "Direct service schedule validation",
                "scope": "Schedule validation",
                "schedule": "quarter-hourly",
            }
        )


def test_schedule_topic_profile_rejects_unsupported_schedule(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore schedule endpoint validation",
                "scope": "Schedule validation",
            },
        )
        scheduled = client.post(
            f"/api/explore/topic-profiles/{created.json()['topic_id']}/schedule",
            json={"schedule": "quarter-hourly"},
        )

    assert created.status_code == 201
    assert scheduled.status_code == 422


def test_scheduled_topic_profiles_list_and_unschedule(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore manageable schedules",
                "scope": "Schedule management",
                "schedule": "weekly",
                "source_selection": {"gmail": True, "reddit": False},
            },
        )
        topic_id = created.json()["topic_id"]
        exploration = database.create_exploration(
            topic_id=topic_id,
            mode="scheduled",
            source_selection={"gmail": True, "reddit": False},
        )
        database.update_exploration_status(
            exploration["exploration_id"],
            status="complete",
            brief_ref="/tmp/fake-explore-brief.html",
        )

        listed = client.get("/api/explore/scheduled-topic-profiles")
        cleared = client.post(
            f"/api/explore/topic-profiles/{topic_id}/schedule",
            json={"schedule": None},
        )
        listed_after_clear = client.get("/api/explore/scheduled-topic-profiles")

    assert created.status_code == 201
    assert listed.status_code == 200
    scheduled_topic = next(item for item in listed.json() if item["topic_id"] == topic_id)
    assert scheduled_topic["schedule"] == "weekly"
    assert scheduled_topic["next_run_at"]
    assert scheduled_topic["latest_exploration"]["exploration_id"] == exploration["exploration_id"]
    assert cleared.status_code == 201
    assert cleared.json()["schedule"] is None
    assert all(item["topic_id"] != topic_id for item in listed_after_clear.json())


def test_pause_and_delete_topic_digest_visibility(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore pauseable digest",
                "scope": "Pause management",
                "schedule": "daily",
                "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
            },
        )
        topic_id = created.json()["topic_id"]

        paused = client.post(f"/api/explore/topic-profiles/{topic_id}/pause")
        public_list = client.get("/api/explore/scheduled-topic-profiles")
        admin_library = client.get("/api/admin/library")
        deleted = client.delete(f"/api/explore/topic-profiles/{topic_id}")
        admin_after_delete = client.get("/api/admin/library")

    assert created.status_code == 201
    assert paused.status_code == 200
    assert paused.json()["profile"]["status"] == "paused"
    assert all(item["topic_id"] != topic_id for item in public_list.json())
    assert any(item["topic_id"] == topic_id for item in admin_library.json()["digests"])
    assert deleted.status_code == 200
    assert deleted.json()["profile"]["deleted"] is True
    assert all(item["topic_id"] != topic_id for item in admin_after_delete.json()["digests"])


def test_soft_delete_exploration_hides_brief_and_can_restore(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        topic = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore deletion undo",
                "scope": "Deletion undo",
                "source_selection": {"web_search": True},
            },
        ).json()
        exploration = database.create_exploration(
            topic_id=topic["topic_id"],
            mode="show_now",
            source_selection={"web_search": True},
        )
        brief_path = tmp_path / "runtime" / "data" / "digest-output" / f"exploration-{exploration['exploration_id']}.html"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text("<html><body>Brief</body></html>", encoding="utf-8")
        database.update_exploration_status(
            exploration["exploration_id"],
            status="complete",
            brief_ref=str(brief_path),
        )

        deleted = client.delete(f"/api/explore/explorations/{exploration['exploration_id']}")
        public_list = client.get("/api/explore/explorations")
        admin_library = client.get("/api/admin/library")
        hidden_brief = client.get(f"/api/explore/explorations/{exploration['exploration_id']}/brief/html")
        restored = client.post(f"/api/explore/explorations/{exploration['exploration_id']}/restore")
        restored_list = client.get("/api/explore/explorations")
        restored_brief = client.get(f"/api/explore/explorations/{exploration['exploration_id']}/brief/html")

    assert deleted.status_code == 200
    assert deleted.json()["exploration"]["deleted_at"]
    assert all(item["exploration_id"] != exploration["exploration_id"] for item in public_list.json())
    assert any(item["exploration_id"] == exploration["exploration_id"] for item in admin_library.json()["deleted_explorations"])
    assert hidden_brief.status_code == 404
    assert restored.status_code == 200
    assert restored.json()["exploration"]["deleted_at"] is None
    assert any(item["exploration_id"] == exploration["exploration_id"] for item in restored_list.json())
    assert restored_brief.status_code == 200


def test_expired_deleted_exploration_purge_removes_stored_content(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    topic = explore.save_topic_profile(
        {
            "statement": "Explore purge",
            "scope": "Purge behavior",
            "source_selection": {"web_search": True},
        }
    )
    exploration = database.create_exploration(
        topic_id=topic["topic_id"],
        mode="show_now",
        source_selection={"web_search": True},
    )
    brief_path = tmp_path / "runtime" / "data" / "digest-output" / f"exploration-{exploration['exploration_id']}.html"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text("<html><body>Brief to purge</body></html>", encoding="utf-8")
    database.update_exploration_status(
        exploration["exploration_id"],
        status="complete",
        brief_ref=str(brief_path),
    )
    database.soft_delete_exploration(exploration["exploration_id"])
    with database.connect() as connection:
        connection.execute(
            "UPDATE explorations SET delete_after = '2000-01-01T00:00:00+00:00' WHERE exploration_id = ?",
            (exploration["exploration_id"],),
        )

    purged = database.purge_expired_deleted_explorations()

    assert purged == 1
    assert database.get_exploration(exploration["exploration_id"]) is None
    assert database.get_topic_profile(topic["topic_id"]) is None
    assert not brief_path.exists()


def test_admin_exploration_issue_details_report_source_and_reason(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        topic = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore requested source issues",
                "scope": "Issue reporting",
                "source_selection": {"web_search": True},
            },
        ).json()
        exploration = database.create_exploration(
            topic_id=topic["topic_id"],
            mode="show_now",
            source_selection={"web_search": True},
        )
        database.update_exploration_progress(
            exploration["exploration_id"],
            progress={
                "built_with_issues": True,
                "requested_source_issues": [
                    {"source_name": "The Daily AI Brief", "reason": "Podcast source could not be resolved"}
                ],
            },
        )
        details = client.get(f"/api/admin/explorations/{exploration['exploration_id']}/issues")

    assert details.status_code == 200
    assert details.json() == {
        "exploration_id": exploration["exploration_id"],
        "built_with_issues": True,
        "issues": [
            {"source_name": "The Daily AI Brief", "reason": "Podcast source could not be resolved"}
        ],
    }


def test_requested_source_matching_tolerates_sparse_candidate_fields() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "Explore approved Gmail newsletters",
            "scope": "Approved Gmail newsletters",
        }
    )
    discovery = DiscoveryResult(
        profile=profile,
        candidates=(
            Candidate(
                adapter="gmail",
                payload=NormalizedPayload(
                    source_name=None,
                    original_url=None,
                    metadata={"sender_name": "Tech Brew", "message_count": 2, "empty": None},
                ),
            ),
        ),
        statuses=(),
    )

    assert explore._requested_source_found(
        adapter="gmail",
        source_name="Tech Brew",
        discovery=discovery,
    )
    assert not explore._requested_source_found(
        adapter="gmail",
        source_name="Unknown Newsletter",
        discovery=discovery,
    )


def test_admin_exploration_issue_details_ignore_filter_decisions(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        topic = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore filtered items",
                "scope": "Filter reporting",
                "source_selection": {"web_search": True},
            },
        ).json()
        exploration = database.create_exploration(
            topic_id=topic["topic_id"],
            mode="show_now",
            source_selection={"web_search": True},
        )
        database.update_exploration_progress(
            exploration["exploration_id"],
            progress={
                "built_with_issues": True,
                "source_audit_issues": [
                    {
                        "source_name": "Old Article",
                        "reason": "Date hints place it outside the requested source window (2026-04-28 06:16 UTC or newer required).",
                    }
                ],
            },
        )
        details = client.get(f"/api/admin/explorations/{exploration['exploration_id']}/issues")

    assert details.status_code == 200
    assert details.json() == {
        "exploration_id": exploration["exploration_id"],
        "built_with_issues": False,
        "issues": [],
    }


def test_run_topic_profile_as_scheduled_mode(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore scheduled mode start",
                "scope": "Run as scheduled",
                "schedule": "daily",
                "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
            },
        )
        topic_id = created.json()["topic_id"]
        run = client.post(
            f"/api/explore/topic-profiles/{topic_id}/run",
            json={"mode": "scheduled"},
        )
        exploration_id = run.json()["exploration"]["exploration_id"]
        scheduled_list = client.get("/api/explore/scheduled-topic-profiles").json()

        poll = client.get(f"/api/explore/explorations/{exploration_id}")
        for _ in range(120):
            poll_body = poll.json()
            if poll_body["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
            poll = client.get(f"/api/explore/explorations/{exploration_id}")

    assert created.status_code == 201
    assert run.status_code == 202
    run_body = run.json()
    assert run_body["exploration"]["mode"] == "scheduled"
    assert poll_body["status"] in {"complete", "failed"}
    assert scheduled_list
    assert any(topic["topic_id"] == topic_id for topic in scheduled_list)


def test_rebuild_route_starts_from_event_loop(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    observed: dict[str, bool] = {}

    def fake_start_rebuild(*_args, **_kwargs) -> dict[str, Any]:
        observed["loop_running"] = asyncio.get_running_loop().is_running()
        return {
            "exploration_id": "exp-1",
            "topic_id": "topic-1",
            "mode": "show_now",
            "status": "running",
        }

    monkeypatch.setattr(explore, "start_rebuild", fake_start_rebuild)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/explore/explorations/exp-1/rebuild", json={})

    assert response.status_code == 202
    assert observed["loop_running"] is True


def test_run_topic_profile_as_scheduled_mode_and_send_email(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    class FakeSend:
        def execute(self) -> dict[str, str]:
            return {"id": "fake-message-id"}

    class FakeMessages:
        def send(self, **_kwargs) -> FakeSend:
            return FakeSend()

    class FakeUsers:
        def messages(self) -> FakeMessages:
            return FakeMessages()

    class FakeService:
        def users(self) -> FakeUsers:
            return FakeUsers()

    monkeypatch.setattr(email_delivery, "_gmail_service", lambda: FakeService())

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        profile = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore scheduled mode email",
                "scope": "Scheduled run and email",
                "schedule": "daily",
                "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
            },
        )
        topic_id = profile.json()["topic_id"]
        run = client.post(
            f"/api/explore/topic-profiles/{topic_id}/run",
            json={"mode": "scheduled"},
        )
        exploration_id = run.json()["exploration"]["exploration_id"]
        poll = client.get(f"/api/explore/explorations/{exploration_id}")
        for _ in range(120):
            poll_body = poll.json()
            if poll_body["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
            poll = client.get(f"/api/explore/explorations/{exploration_id}")

        scheduled_list = client.get("/api/explore/scheduled-topic-profiles").json()
        sent = client.post(
            f"/api/explore/explorations/{exploration_id}/email",
            json={"recipient_email": "adrian@example.com"},
        )
        exploration = client.get(f"/api/explore/explorations/{exploration_id}")

    assert profile.status_code == 201
    assert run.status_code == 202
    assert poll_body["status"] == "complete"
    scheduled_topic = next(item for item in scheduled_list if item["topic_id"] == topic_id)
    assert scheduled_topic["latest_exploration"]["exploration_id"] == exploration_id
    assert scheduled_topic["latest_exploration"]["mode"] == "scheduled"
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"
    assert sent.json()["message_id"] == "fake-message-id"
    assert exploration.json()["emailed"] is True


def test_create_topic_profile_api_ignores_invalid_model_payload_values(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Explore invalid model endpoint payload",
                "scope": "Endpoint sanitization checks",
                "models": {
                    "brief": 123,
                    "refinement": {"bad": "model"},
                    "unexpected": ["ignored"],
                },
            },
        )

    assert created.status_code == 201
    body = created.json()
    assert body["profile"]["models"]["brief"] is None
    assert body["profile"]["models"]["refinement"] is None


def test_save_topic_profile_sanitizes_malformed_models(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    profile = explore.save_topic_profile(
        {
            "statement": "Malformed model payload sanitization",
            "models": {
                "brief": {"id": "ignored"},
                "refinement": None,
                "unknown": "ignored",
            },
        }
    )
    assert profile["profile"]["models"]["brief"] is None
    assert profile["profile"]["models"]["refinement"] is None


def test_refinement_session_just_go_now_uses_defaults(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={"statement": "Explore local AI infrastructure"},
        )
        finalized = client.post(
            f"/api/explore/refinement-sessions/{started.json()['session_id']}/messages",
            json={"just_go_now": True},
        )

    assert finalized.status_code == 200
    body = finalized.json()
    profile = body["topic_profile"]["profile"]
    assert body["status"] == "finalized"
    assert profile["scope"] == "Explore local AI infrastructure"
    assert profile["depth"] == "informed-generalist"
    assert profile["recency_weighting"] == "recent"


def test_refinement_session_auto_prefills_depth_from_statement(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        started = client.post(
            "/api/explore/refinement-sessions",
            json={"statement": "Technical build notes for local model deployment"},
        )
        session_id = started.json()["session_id"]
        assert started.status_code == 201
        post_scope = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "Hands-on implementation and architecture patterns"},
        )

    assert post_scope.status_code == 200
    assert post_scope.json()["profile"]["depth"] == "practitioner"
    assert post_scope.json()["pending_field"] == "related_interests"

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        followup = client.post(
            f"/api/explore/refinement-sessions/{session_id}/messages",
            json={"answer": "none"},
        )

    assert followup.status_code == 200
    assert followup.json()["pending_field"] == "depth"


def test_explore_digest_core_uses_profile_brief_model(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()

    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "true")
    monkeypatch.setenv("MORNING_DISPATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_MODEL", "global-brief")

    profile = TopicProfile.from_dict(
        {
            "statement": "AI operations",
            "scope": "AI operations update",
            "models": {"brief": "topic-brief-model", "refinement": None},
        },
    )

    observed = {}

    def fake_apply_cached_model_enrichments(results, *, model_name, limit):
        observed["cached_lookup_model"] = model_name
        return results

    async def fake_refine_ranked_articles_with_model(
        results,
        *,
        model_client=None,
        model_max_items=None,
        inference_run_id=None,
        metrics_mode="single",
    ):
        observed["refine_model"] = getattr(getattr(model_client, "config", None), "model", None)
        observed["refine_model_max_items"] = model_max_items
        return results

    async def fake_apply_editorial_decisions(
        digest: dict,
        results: list,
        *,
            model_client=None,
            reasoning_callback=None,
            inference_run_id=None,
            max_candidates=None,
    ):
        observed["editorial_model"] = getattr(getattr(model_client, "config", None), "model", None)
        return results, []

    async def fake_apply_critic_repairs(
        digest: dict,
        payloads: list,
        results: list,
        *,
            model_client=None,
            reasoning_callback=None,
            inference_run_id=None,
            max_articles=None,
            max_newsletter_records=None,
    ):
        observed["critic_model"] = getattr(getattr(model_client, "config", None), "model", None)
        return results, []

    def fake_cache_model_enrichments(results, *, model_name):
        observed["cached_write_model"] = model_name
        return 0

    monkeypatch.setattr(database, "apply_cached_model_enrichments", fake_apply_cached_model_enrichments)
    monkeypatch.setattr(database, "cache_model_enrichments", fake_cache_model_enrichments)
    monkeypatch.setattr(explore, "refine_ranked_articles_with_model", fake_refine_ranked_articles_with_model)
    monkeypatch.setattr(explore, "apply_editorial_decisions", fake_apply_editorial_decisions)
    monkeypatch.setattr(explore, "apply_critic_repairs", fake_apply_critic_repairs)

    result = asyncio.run(
        explore._run_digest_core(
            profile=profile,
            payloads=[],
            fetched_articles=[],
            inference_run_id="run-1",
            progress={"pipeline": {}},
            persist=lambda: None,
        )
    )

    assert result == []
    assert observed["cached_lookup_model"] == "topic-brief-model"
    assert observed["cached_write_model"] == "topic-brief-model"
    assert observed["refine_model"] == "topic-brief-model"
    assert observed["refine_model_max_items"] == 150
    assert observed["editorial_model"] == "topic-brief-model"
    assert observed["critic_model"] == "topic-brief-model"


def test_topic_profile_endpoints_include_promoted_sources(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/explore/topic-profiles",
            json={
                "statement": "Track local AI newsletters and podcasts",
                "scope": "Signals worth recurring",
                "source_selection": {"gmail": True, "reddit": True, "podcasts": True, "web_search": True},
            },
        )
        topic_id = created.json()["topic_id"]

        database.add_promoted_source(
            topic_id=topic_id,
            adapter="reddit",
            ref="r/localAI",
            has_feed=False,
            feed_url=None,
        )
        database.add_promoted_source(
            topic_id=topic_id,
            adapter="podcasts",
            ref="Practical AI Podcast",
            has_feed=True,
            feed_url="https://podcast.example.com/feed",
        )

        fetched = client.get(f"/api/explore/topic-profiles/{topic_id}")
        listed = client.get("/api/explore/topic-profiles")

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert listed.status_code == 200

    promoted = fetched.json().get("profile", {}).get("promoted_sources", [])
    assert len(promoted) == 2
    assert any(
        source["adapter"] == "reddit"
        and source["ref"] == "r/localAI"
        and source["has_feed"] is False
        for source in promoted
    )
    assert any(
        source["adapter"] == "podcasts"
        and source["ref"] == "Practical AI Podcast"
        and source["has_feed"] is True
        and source["feed_url"] == "https://podcast.example.com/feed"
        for source in promoted
    )

    listed_profiles = listed.json()
    topic_row = next((item for item in listed_profiles if item["topic_id"] == topic_id), None)
    assert topic_row is not None
    assert topic_row["profile"]["promoted_sources"] == promoted


def test_run_show_now_marks_exploration_failed_on_error(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    profile = database.upsert_topic_profile(
        {
            "statement": "Explore failed runs",
            "scope": "Failure handling",
            "depth": "informed-generalist",
            "recency_weighting": "balanced",
            "exclusions": [],
            "source_selection": {"gmail": False, "reddit": False, "podcasts": False, "web_search": False},
        }
    )

    async def broken_discovery_run(self, *_args, **_kwargs):
        raise RuntimeError("discovery service unavailable")

    monkeypatch.setattr(DiscoveryRunner, "run", broken_discovery_run)

    with pytest.raises(RuntimeError, match="discovery service unavailable"):
        asyncio.run(explore.run_show_now(topic_id=profile["topic_id"], source_selection={"gmail": False}))

    failed = database.get_latest_exploration(topic_id=profile["topic_id"], mode="show_now", status="failed")
    assert failed is not None
    assert failed["status"] == "failed"
