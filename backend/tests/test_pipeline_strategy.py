from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bs4 import BeautifulSoup

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery.adapters import _promoted_refs, _source_plan_refs
from backend.agents.discovery import query_refiner
from backend.agents.discovery.runner import _expand_profile_queries, datetime_now_year
from backend.agents.discovery.types import SourceAdapterContext, TopicProfile
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.db import database
from backend.app.services import brief_settings, explore


def _result(
    *,
    title: str,
    url: str,
    source_type: str,
    score: float = 0.5,
    tier: str = "main",
    status: str = "fetched",
    web: bool = False,
) -> ArticleFetchResult:
    metadata = {"title": title}
    if web:
        # Force the web_search adapter mapping for gmail_link payloads.
        metadata["search_provider"] = "tavily"
    payload = NormalizedPayload(
        source_type=source_type,
        source_name="Test Source",
        original_url=url,
        raw_text="Body text about AI agents and infrastructure.",
        published_at=datetime.now(UTC).isoformat(),
        metadata=metadata,
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=url,
        final_url=url,
        canonical_url=url,
        title=title,
        text="Body text about AI agents and infrastructure.",
        excerpt="Body text about AI agents and infrastructure.",
        editor_summary="Body text about AI agents and infrastructure.",
        domain="example.com",
        status=status,
        link_score=score,
        tier=tier,
    )


# ---------------------------------------------------------------------------
# Item 7: unified limits + percentage presets
# ---------------------------------------------------------------------------


def test_percent_presets_match_historical_tiers() -> None:
    assert brief_settings._percent_presets(20) == {"max": 20, "large": 16, "medium": 12, "focused": 8}
    assert brief_settings._percent_presets(5) == {"max": 5, "large": 4, "medium": 3, "focused": 2}
    assert brief_settings._percent_presets(40) == {"max": 40, "large": 32, "medium": 24, "focused": 16}


def test_source_max_is_single_source_of_truth() -> None:
    assert brief_settings.source_inclusion_max("reddit") == 30
    assert brief_settings.source_inclusion_max("web_search") == 40
    assert brief_settings.source_inclusion_max("youtube") == 20
    # Unknown sources fall back to the default ceiling.
    assert brief_settings.source_inclusion_max("mystery") == brief_settings.DEFAULT_PER_SOURCE_MAX


def test_source_min_items_defaults_and_overrides() -> None:
    assert brief_settings.source_min_items("podcasts", {}) == 5
    assert brief_settings.source_min_items("web_search", {}) == brief_settings.DEFAULT_SOURCE_FLOOR
    assert brief_settings.source_min_items("web_search", {"min_items": {"web_search": 3}}) == 3
    # Floor can never exceed the source ceiling.
    assert brief_settings.source_min_items("youtube", {"min_items": {"youtube": 999}}) == 20


def test_screening_sample_randomizes_large_candidate_pools(monkeypatch) -> None:
    candidates = list(range(query_refiner._SCREENING_MAX_CANDIDATES_PER_SOURCE + 25))
    observed: dict[str, int] = {}

    def fake_sample(population, k):
        observed["population"] = len(population)
        observed["sample_size"] = k
        return list(population[-k:])

    monkeypatch.setattr(query_refiner.random, "sample", fake_sample)

    sampled = query_refiner._screening_sample(candidates)

    assert observed == {
        "population": len(candidates),
        "sample_size": query_refiner._SCREENING_MAX_CANDIDATES_PER_SOURCE,
    }
    assert sampled == candidates[-query_refiner._SCREENING_MAX_CANDIDATES_PER_SOURCE :]


# ---------------------------------------------------------------------------
# Item 3: generalized per-source inclusion floor (not just podcasts)
# ---------------------------------------------------------------------------


def test_inclusion_floor_revives_any_source(monkeypatch, tmp_path) -> None:
    profile = TopicProfile.from_dict({
        "topic_id": "topic-floor",
        "statement": "AI agents and infrastructure",
        "source_selection": {"web_search": True},
        "content_limits": {
            "min_items": {"web_search": 2},
            "per_source": {"web_search": 10},
        },
    })
    results = [
        _result(title="Dropped Safe 1", url="https://example.com/w1", source_type="gmail_link", score=0.40, tier="dropped", web=True),
        _result(title="Dropped Safe 2", url="https://example.com/w2", source_type="gmail_link", score=0.30, tier="dropped", web=True),
        _result(title="Dropped Low", url="https://example.com/w3", source_type="gmail_link", score=0.10, tier="dropped", web=True),
    ]
    enforced = explore._enforce_inclusion_limits(profile, results)
    active = {r.title for r in enforced if r.tier != "dropped"}
    assert active == {"Dropped Safe 1", "Dropped Safe 2"}


def test_inclusion_floor_skipped_when_source_disabled() -> None:
    profile = TopicProfile.from_dict({
        "topic_id": "topic-floor-off",
        "statement": "AI agents",
        "source_selection": {"web_search": False},
        "content_limits": {"min_items": {"web_search": 2}},
    })
    results = [
        _result(title="Dropped Safe", url="https://example.com/x1", source_type="gmail_link", score=0.40, tier="dropped", web=True),
    ]
    enforced = explore._enforce_inclusion_limits(profile, results)
    assert all(r.tier == "dropped" for r in enforced)


# ---------------------------------------------------------------------------
# Item 6: promoted sources feed back into query construction
# ---------------------------------------------------------------------------


def test_promoted_sources_feed_query_construction() -> None:
    profile = TopicProfile.from_dict({
        "topic_id": "topic-promote",
        "statement": "AI agents",
        "promoted_sources": [
            {"adapter": "web_search", "ref": "anthropic.com agent updates"},
            {"adapter": "podcasts", "ref": "Latent Space"},
        ],
    })
    assert _promoted_refs(profile, "web_search") == ["anthropic.com agent updates"]
    refs = _source_plan_refs(profile, "web_search")
    assert "anthropic.com agent updates" in refs


# ---------------------------------------------------------------------------
# Item 1: proactive query expansion folds affiliated angles into source queries
# ---------------------------------------------------------------------------


def test_expand_profile_queries_augments_selected_sources(monkeypatch) -> None:
    class FakeClient:
        async def complete_json(self, system: str, prompt: str, max_tokens: int = 600):
            return {"refined_queries": ["agent reliability benchmarks", "LLM eval tooling"]}

    class FakeResolution:
        client = FakeClient()

    from backend.app.services import model_routing

    monkeypatch.setattr(model_routing, "client_for_agent", lambda *a, **k: FakeResolution())

    profile = TopicProfile.from_dict({
        "topic_id": "topic-expand",
        "statement": "AI agents",
        "scope": "AI agents",
        "search_queries": ["AI agents"],
        "source_selection": {"web_search": True, "reddit": True, "gmail": False},
    })
    context = SourceAdapterContext(exploration_id="explore-expand", candidate_limit=10)
    expanded = asyncio.run(
        _expand_profile_queries(profile, profile.source_selection, context, low_yield=False)
    )
    # Affiliated angles are added to every SELECTED source's queries...
    assert "agent reliability benchmarks" in expanded.source_queries["web_search"]
    assert "LLM eval tooling" in expanded.source_queries["reddit"]
    # ...but not to deselected sources, and not to the global topic text (gate stays tight).
    assert "gmail" not in expanded.source_queries
    assert "agent reliability benchmarks" not in expanded.discovery_text()


def test_expand_profile_queries_fails_open_without_client(monkeypatch) -> None:
    from backend.app.services import model_routing

    class NoResolution:
        client = None

    monkeypatch.setattr(model_routing, "client_for_agent", lambda *a, **k: NoResolution())
    profile = TopicProfile.from_dict({
        "topic_id": "topic-noexpand",
        "statement": "AI agents",
        "search_queries": ["AI agents"],
        "source_selection": {"web_search": True},
    })
    context = SourceAdapterContext(exploration_id="explore-noexpand", candidate_limit=10)
    expanded = asyncio.run(
        _expand_profile_queries(profile, profile.source_selection, context, low_yield=False)
    )
    assert expanded is profile


def test_expand_profile_queries_scrubs_stale_years_even_without_client(monkeypatch) -> None:
    # Even when expansion produces nothing (no client), stored stale-year queries must
    # be sanitized so they don't pull out-of-window content the recency filter discards.
    from backend.app.services import model_routing

    class NoResolution:
        client = None

    monkeypatch.setattr(model_routing, "client_for_agent", lambda *a, **k: NoResolution())
    profile = TopicProfile.from_dict({
        "topic_id": "topic-stale",
        "statement": "Mac local AI",
        "source_queries": {"web_search": ["best local AI tools for Mac 2024", "WWDC 2025 recap"]},
        "source_selection": {"web_search": True},
        "lookback_hours": 168,
    })
    context = SourceAdapterContext(exploration_id="explore-stale", candidate_limit=10, lookback_hours=168)
    expanded = asyncio.run(
        _expand_profile_queries(profile, profile.source_selection, context, low_yield=False)
    )
    joined = " ".join(expanded.source_queries["web_search"])
    assert "2024" not in joined and "2025" not in joined
    assert str(datetime_now_year()) in joined


# ---------------------------------------------------------------------------
# Items 4 + 5: Top Stories (cross-source) and per-source sections in the brief
# ---------------------------------------------------------------------------


def test_brief_has_top_stories_and_per_source_sections() -> None:
    results = [
        _result(title="Web lead", url="https://example.com/web-0", source_type="gmail_link", score=0.95, tier="lead", web=True),
        _result(title="Web 1", url="https://example.com/web-1", source_type="gmail_link", score=0.90, web=True),
        _result(title="Web 2", url="https://example.com/web-2", source_type="gmail_link", score=0.88, web=True),
        _result(title="Web 3", url="https://example.com/web-3", source_type="gmail_link", score=0.86, web=True),
        _result(title="Web 4", url="https://example.com/web-4", source_type="gmail_link", score=0.84, web=True),
        _result(title="Web 5", url="https://example.com/web-5", source_type="gmail_link", score=0.82, web=True),
        _result(title="Reddit 1", url="https://reddit.com/r/ai/1", source_type="reddit_post", score=0.55),
        _result(title="Reddit 2", url="https://reddit.com/r/ai/2", source_type="reddit_post", score=0.50),
        _result(title="Reddit 3", url="https://reddit.com/r/ai/3", source_type="reddit_post", score=0.45),
        _result(title="Podcast 1", url="https://podcasts.example.com/ep-1", source_type="podcast_episode", score=0.60),
    ]
    html = database.render_ingested_issue(
        "Strategy Brief",
        "Cross-source mix",
        [],
        results,
        lookback_hours=24,
    )
    soup = BeautifulSoup(html, "html.parser")

    # Item 5: a single cross-source Top Stories section sits above everything.
    assert soup.select_one(".top-stories-section") is not None
    assert "Across all sources" in html

    # Item 4: each non-media source gets its own labeled section.
    source_headings = {h.get_text(strip=True) for h in soup.select(".source-section h2")}
    assert "Reddit" in source_headings

    # Podcast media renders in its own dedicated "Listen" section.
    media_headings = {h.get_text(strip=True) for h in soup.select(".media-section h2")}
    assert "Listen" in media_headings

    # Exactly one lead block, and the top-stories list mixes sources beyond the lead.
    assert len(soup.select(".lead-block")) == 1
