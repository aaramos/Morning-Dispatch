"""Regression tests for the source-content remediation work.

Covers: locale-aware date parsing, recency demote-not-delete + per-source floor
revival (P0), source-aware fetch budget (P1), origin-based brief labels + empty
per-source real estate (P2), per-source broaden-on-empty (P3), reporting honesty
for revived items (P4), podcast transcript-feed + transcription budget (P5), and
the foreign-media English-gate exemption.
"""
from __future__ import annotations

import asyncio

from backend.agents.digestor import podcast
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery import adapters, query_refiner, runner
from backend.agents.discovery.markets import resolve_tickers_from_text
from backend.agents.discovery.types import (
    AdapterStatus,
    Candidate,
    DiscoveryResult,
    SourceAdapterContext,
    TopicProfile,
)
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.db import database
from backend.app.services import explore, reporting


def _profile(**overrides) -> TopicProfile:
    payload = {
        "topic_id": "t1",
        "statement": "AI infrastructure pick and shovel companies",
        "scope": "AI infrastructure",
        "source_selection": {"web_search": True, "foreign_media": True, "gmail": True},
        "content_limits": {"per_source": {"web_search": 25, "foreign_media": 25, "gmail": 25}},
    }
    payload.update(overrides)
    return TopicProfile.from_dict(payload)


def _result(
    *,
    source_type: str,
    url: str,
    title: str = "Story",
    tier: str = "main",
    link_score: float = 0.6,
    status: str = "fetched",
    metadata: dict | None = None,
    payload_metadata: dict | None = None,
) -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type=source_type,
        source_name=title,
        original_url=url,
        metadata=payload_metadata or {},
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=url,
        final_url=url,
        canonical_url=url,
        title=title,
        text="body text about AI infrastructure",
        excerpt="excerpt",
        domain="example.com",
        status=status,
        tier=tier,
        link_score=link_score,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# DATE: locale-aware parsing
# ---------------------------------------------------------------------------


def test_locale_dates_parse() -> None:
    assert explore._date_from_text("2026年4月25日 発表").date().isoformat() == "2026-04-25"
    assert explore._date_from_text("2026년 4월 25일 보도").date().isoformat() == "2026-04-25"
    assert explore._date_from_text("등록 2026.04.25 09:30").date().isoformat() == "2026-04-25"
    assert explore._date_from_text("Published April 25, 2026").date().isoformat() == "2026-04-25"
    assert explore._date_from_text("no date here at all") is None


# ---------------------------------------------------------------------------
# P0: recency demote-not-delete + floor revival
# ---------------------------------------------------------------------------


def test_recency_filter_demotes_into_reserve() -> None:
    profile = _profile()
    stale = _result(
        source_type="gmail_link",
        url="https://ex.com/old/2020/01/01/x",
        title="Old web story",
        payload_metadata={"search_query": "ai"},
    )
    kept, issues, reserve = explore._apply_source_window_filter(
        profile, [stale], lookback_hours=24
    )
    assert kept == []
    assert len(issues) == 1
    assert len(reserve) == 1
    assert reserve[0].metadata.get("out_of_window") is True


def test_floor_revives_reserve_only_when_source_empty() -> None:
    profile = _profile()
    reserve_item = _result(
        source_type="foreign_web",
        url="https://example.kr/n/1",
        title="HBM 수요",
        tier="dropped",
        metadata={"out_of_window": True, "out_of_window_published_at": "2026-01-01T00:00:00+00:00"},
    )
    web_in = _result(source_type="gmail_link", url="https://ex.com/a", payload_metadata={"search_query": "ai"})

    revived = explore._enforce_inclusion_limits(profile, [web_in], recency_reserve=[reserve_item])
    foreign_active = [r for r in revived if explore._result_adapter(r) == "foreign_media" and r.tier != "dropped"]
    assert len(foreign_active) == 1
    assert foreign_active[0].metadata.get("served_once") is True

    foreign_fresh = _result(source_type="foreign_web", url="https://example.kr/n/2", title="Fresh")
    no_pad = explore._enforce_inclusion_limits(profile, [foreign_fresh], recency_reserve=[reserve_item])
    foreign_active2 = [r for r in no_pad if explore._result_adapter(r) == "foreign_media" and r.tier != "dropped"]
    assert len(foreign_active2) == 1  # reserve NOT added when in-window content exists

    high_floor = _profile(content_limits={"min_items": {"foreign_media": 2}})
    no_stale_padding = explore._enforce_inclusion_limits(
        high_floor, [foreign_fresh], recency_reserve=[reserve_item]
    )
    foreign_active3 = [
        r for r in no_stale_padding if explore._result_adapter(r) == "foreign_media" and r.tier != "dropped"
    ]
    assert len(foreign_active3) == 1  # stale reserve is not padding for a non-empty source


# ---------------------------------------------------------------------------
# P1: source-aware fetch budget
# ---------------------------------------------------------------------------


def _cand(adapter: str, source_type: str, i: int, meta: dict | None = None) -> Candidate:
    return Candidate(
        adapter=adapter,
        score=0.5,
        payload=NormalizedPayload(
            source_type=source_type,
            source_name=f"{adapter}{i}",
            original_url=f"https://{adapter}.com/{i}",
            metadata=meta or {},
        ),
    )


def test_fetch_budget_reserves_web_foreign_under_newsletter_flood() -> None:
    profile = _profile()
    cands = []
    cands += [_cand("gmail", "gmail", i) for i in range(40)]  # direct bodies
    cands += [_cand("gmail", "gmail_link", i) for i in range(200)]  # newsletter links (HTTP)
    cands += [_cand("web_search", "gmail_link", i, {"search_query": "q"}) for i in range(50)]
    cands += [_cand("foreign_media", "foreign_web", i, {"source_language": "ko"}) for i in range(30)]

    payloads, budgets = explore._select_fetch_payloads_for_budget(cands, profile=profile, max_articles=20)
    assert budgets.get("web_search", 0) >= 5
    assert budgets.get("foreign_media", 0) >= 5
    # Direct gmail bodies bypass the HTTP budget. They oversample to 2x the inclusion
    # cap (25 -> 50) but are bounded by what's available (40), so all 40 enter
    # enrichment; the inclusion cap is re-applied downstream.
    assert sum(1 for p in payloads if p.source_type == "gmail") == 40


def test_fetch_budget_oversamples_beyond_inclusion_cap_when_budget_allows() -> None:
    # With ample global budget, each HTTP lane fetches up to 2x its inclusion cap
    # (bounded by availability) so recency/audit attrition can't starve it. The
    # final brief is still capped at the inclusion limit by _enforce_inclusion_limits.
    profile = _profile()
    cands = [_cand("web_search", "gmail_link", i, {"search_query": "q"}) for i in range(50)]
    cands += [_cand("foreign_media", "foreign_web", i, {"source_language": "ko"}) for i in range(30)]
    _payloads, budgets = explore._select_fetch_payloads_for_budget(cands, profile=profile, max_articles=250)
    # web: oversample 2x25=50, bounded by 50 available -> 50 (was capped at 25 before).
    assert budgets["web_search"] == 50
    # foreign: oversample cap 50 but only 30 available -> 30.
    assert budgets["foreign_media"] == 30


def test_fetch_oversample_still_bounded_by_global_budget() -> None:
    # The oversample headroom never exceeds the global article-fetch budget.
    profile = _profile()
    cands = [_cand("web_search", "gmail_link", i, {"search_query": "q"}) for i in range(80)]
    cands += [_cand("foreign_media", "foreign_web", i, {"source_language": "ko"}) for i in range(80)]
    _payloads, budgets = explore._select_fetch_payloads_for_budget(cands, profile=profile, max_articles=30)
    assert sum(budgets.values()) <= 30


def test_recency_prescreen_drops_known_stale_before_budget() -> None:
    # Known-stale candidates (provider date older than the cutoff) are removed
    # before the fetch budget, so the budget targets the in-window pool instead
    # of being spent on items recency would drop right after fetching them.
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=168)
    fresh = now - timedelta(hours=2)
    stale = now - timedelta(days=30)
    profile = _profile()
    cands = []
    cands += [
        _cand("web_search", "gmail_link", i, {"search_query": "q", "published_at": stale.isoformat()})
        for i in range(40)
    ]
    cands += [
        _cand("web_search", "gmail_link", 100 + i, {"search_query": "q", "published_at": fresh.isoformat()})
        for i in range(10)
    ]
    # Undated candidates must survive the pre-screen (date resolved post-fetch).
    cands += [_cand("web_search", "gmail_link", 200 + i, {"search_query": "q"}) for i in range(5)]

    payloads, _budgets = explore._select_fetch_payloads_for_budget(
        cands, profile=profile, max_articles=250, cutoff=cutoff
    )
    selected_dates = {p.metadata.get("published_at") for p in payloads}
    assert stale.isoformat() not in selected_dates  # known-stale pre-dropped
    assert fresh.isoformat() in selected_dates  # in-window kept
    # 10 fresh + 5 undated survive the screen; none of the 40 stale do.
    assert len(payloads) == 15


def test_recency_prescreen_skipped_when_no_cutoff() -> None:
    # With no bounded window (all-available), nothing is pre-dropped on date.
    from datetime import UTC, datetime, timedelta

    stale = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    profile = _profile()
    cands = [_cand("web_search", "gmail_link", i, {"search_query": "q", "published_at": stale}) for i in range(5)]
    payloads, _ = explore._select_fetch_payloads_for_budget(cands, profile=profile, max_articles=250, cutoff=None)
    assert len(payloads) == 5


# ---------------------------------------------------------------------------
# P2: origin labels + empty per-source real estate
# ---------------------------------------------------------------------------


def test_origin_labels_separate_web_foreign_gmail() -> None:
    web = _result(source_type="gmail_link", url="https://ex.com/w", payload_metadata={"search_query": "ai"})
    newsletter = _result(source_type="gmail_link", url="https://ex.com/n", title="Newsletter link")
    foreign = _result(source_type="foreign_web", url="https://ex.kr/f")
    translated = _result(
        source_type="gmail_link",
        url="https://ex.com/t",
        metadata={"translation": {"translated": True, "source_language": "th"}},
    )
    assert database._origin_source_label(web) == "Web"
    assert database._origin_source_label(newsletter) == "Gmail"
    assert database._origin_source_label(foreign) == "Foreign Media"
    assert database._origin_source_label(translated) == "Web"


def test_empty_source_note_only_with_selection() -> None:
    web = _result(source_type="gmail_link", url="https://ex.com/w", payload_metadata={"search_query": "ai"})
    selection = {"web_search": True, "foreign_media": True, "reddit": True}
    html_with = database.render_ingested_issue(
        "Brief", "snap", [], [web], lookback_hours=24, source_selection=selection
    )
    assert "source-section-empty" in html_with
    assert "Foreign Media" in html_with  # the empty selected source gets a labeled block
    # Without an explicit selection, no empty blocks are emitted (legacy behavior).
    html_without = database.render_ingested_issue("Brief", "snap", [], [web], lookback_hours=24)
    assert "source-section-empty" not in html_without


# ---------------------------------------------------------------------------
# P3: per-source broaden on empty
# ---------------------------------------------------------------------------


def _discovery(profile: TopicProfile, statuses: list[AdapterStatus], candidates=()) -> DiscoveryResult:
    return DiscoveryResult(profile=profile, candidates=tuple(candidates), statuses=tuple(statuses))


def test_missing_candidate_sources_detected() -> None:
    profile = _profile()
    discovery = _discovery(
        profile,
        [
            AdapterStatus(name="foreign_media", status="completed", candidate_count=0),
            AdapterStatus(name="web_search", status="completed", candidate_count=5),
            AdapterStatus(name="podcasts", status="timed_out", candidate_count=0),
        ],
    )
    missing = explore._selected_sources_missing_candidates(
        discovery=discovery, source_selection=profile.source_selection
    )
    assert missing == ["foreign_media"]  # completed-with-nothing only; timeouts excluded


def test_broaden_seeds_starved_lanes(monkeypatch) -> None:
    class FakeClient:
        async def complete_json(self, *, system, prompt, max_tokens):
            return {"search_queries": ["AI infrastructure", "data center buildout"]}

    class Res:
        client = FakeClient()

    monkeypatch.setattr(explore.model_routing, "client_for_agent", lambda *a, **k: Res())
    profile = _profile(source_queries={"web_search": ["AI capex"]})
    out = asyncio.run(
        explore.broaden_queries_with_agent(profile, starved_sources=["foreign_media", "web_search"])
    )
    assert "AI infrastructure" in out.source_queries["foreign_media"]
    assert "AI infrastructure" in out.source_queries["web_search"]
    assert "AI capex" in out.source_queries["web_search"]  # existing preserved


def test_gmail_adapter_uses_profile_approved_senders(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    async def fake_fetch_newsletters(*, digest_id, sender_allowlist, lookback_hours, db_path):
        captured["senders"] = list(sender_allowlist)
        return [
            NormalizedPayload(
                source_type="gmail",
                source_name="The Deep View",
                raw_text="Newsletter body about Apple Intelligence.",
                metadata={"sender_email": sender_allowlist[0]},
            )
        ]

    monkeypatch.setattr(adapters.database, "approved_gmail_senders", lambda: [])
    monkeypatch.setattr(adapters, "fetch_newsletters", fake_fetch_newsletters)
    profile = _profile(
        gmail_rules={"include_senders": ["Newsletter@TheDeepView.co", "bad-value"]},
        source_selection={"gmail": True},
    )

    candidates = asyncio.run(
        adapters.GmailSourceAdapter().query(
            profile,
            SourceAdapterContext(exploration_id="gmail-profile-test", lookback_hours=168, candidate_limit=10),
        )
    )

    assert captured["senders"] == ["newsletter@thedeepview.co"]
    assert len(candidates) == 1


def test_bounded_recency_sanitizes_stale_years_before_discovery() -> None:
    profile = _profile(
        search_queries=["Apple WWDC 2024 AI announcements summary"],
        source_queries={
            "web_search": ["best AI productivity apps for Mac 2024"],
            "podcasts": ["consumer AI trends 2024"],
        },
        direct_episode_queries=["WWDC 2024 Apple AI"],
        related_episode_queries=["consumer AI trends 2024"],
    )

    sanitized = runner._sanitize_bounded_recency_queries(profile, 168)
    joined = " ".join([
        *sanitized.search_queries,
        *sanitized.source_queries["web_search"],
        *sanitized.source_queries["podcasts"],
        *sanitized.direct_episode_queries,
        *sanitized.related_episode_queries,
    ])

    assert "2024" not in joined
    assert str(runner.datetime_now_year()) in joined


def test_market_ticker_resolution_ignores_plain_words_and_years() -> None:
    assert resolve_tickers_from_text("I WWDC 2024 256 GB RAM OS MLX UI UX GUI") == []
    assert resolve_tickers_from_text("Apple Intelligence on Mac") == ["AAPL"]


def test_screening_preserves_selected_podcast_when_model_drops_all(monkeypatch) -> None:
    payload = NormalizedPayload(
        source_type="podcast_episode",
        source_name="AI & I",
        raw_text="Adjacent conversation about Apple Intelligence and product strategy.",
        original_url="https://podcasts.example.com/episode",
        metadata={"title": "Adjacent AI conversation", "podcast_title": "AI & I"},
    )
    candidate = Candidate(adapter="podcasts", payload=payload, score=0.2)

    class FakeClient:
        async def complete_json(self, **_kwargs):
            return {"decisions": [{"id": payload.id, "decision": "drop"}]}

    class Res:
        client = FakeClient()

    monkeypatch.setattr(query_refiner.model_routing, "client_for_agent", lambda *a, **k: Res())
    exclusions: list[dict] = []
    profile = _profile(source_selection={"podcasts": True})

    screened = asyncio.run(query_refiner.screen_candidates(profile, [candidate], exclusions=exclusions))

    assert screened == [candidate]
    assert screened[0].payload.metadata["screening_preserved_low_yield"] is True
    assert exclusions == []


def test_broaden_updates_podcast_strategy_for_starved_podcast(monkeypatch) -> None:
    class FakeClient:
        async def complete_json(self, *, system, prompt, max_tokens):
            return {
                "search_queries": ["AI agents", "developer tools"],
                "source_queries": {"podcasts": ["AI"]},
                "direct_episode_queries": ["coding agents"],
                "related_episode_queries": ["developer tools"],
                "priority_terms": ["OpenAI"],
                "negative_constraints": ["crypto"],
            }

    class Res:
        client = FakeClient()

    monkeypatch.setattr(explore.model_routing, "client_for_agent", lambda *a, **k: Res())
    profile = _profile(
        source_selection={"podcasts": True},
        source_queries={"podcasts": ["agent interviews"]},
        direct_episode_queries=["AI agents"],
    )
    out = asyncio.run(explore.broaden_queries_with_agent(profile, starved_sources=["podcasts"]))

    assert "AI" in out.source_queries["podcasts"]
    assert "AI agents" in out.direct_episode_queries
    assert "coding agents" in out.direct_episode_queries
    assert "developer tools" in out.related_episode_queries
    assert "OpenAI" in out.priority_terms
    assert "crypto" in out.negative_constraints


def test_strategy_repair_audit_records_changes_and_retry_result() -> None:
    before = _profile(
        source_selection={"podcasts": True},
        source_queries={"podcasts": ["agent interviews"]},
        related_episode_queries=["legacy tooling"],
    )
    after = _profile(
        source_selection={"podcasts": True},
        search_queries=["AI agents", "developer tools"],
        source_queries={"podcasts": ["agent interviews", "AI"]},
        direct_episode_queries=["coding agents"],
        related_episode_queries=["legacy tooling", "developer tools"],
        priority_terms=["OpenAI"],
    )
    progress: dict = {}

    explore._record_strategy_repair(
        progress,
        before=before,
        after=after,
        attempt=1,
        retry_attempt=2,
        included_count=0,
        target_yield=3,
        starved_sources=["podcasts"],
    )

    repair = progress["strategy_repairs"][0]
    assert repair["status"] == "retrying"
    assert repair["trigger"] == "source_starvation"
    assert repair["changed"] is True
    assert repair["changed_sources"] == ["podcasts"]
    assert repair["podcast_strategy_changed"] is True
    assert repair["persisted_to_topic_profile"] is False
    assert repair["after_podcast_strategy"]["direct_episode_queries"] == ["coding agents"]

    explore._update_strategy_repair_result(
        progress,
        attempt=2,
        included_count=4,
        starved_sources=[],
    )
    assert repair["status"] == "completed"
    assert repair["retry_result"]["status"] == "improved"
    assert repair["retry_result"]["included_count"] == 4


# ---------------------------------------------------------------------------
# P4: reporting honesty for revived items
# ---------------------------------------------------------------------------


def test_revived_reserve_item_reports_included_not_recency() -> None:
    profile = _profile()
    url = "https://example.kr/n/1"
    cand = Candidate(
        adapter="foreign_media",
        score=0.5,
        payload=NormalizedPayload(source_type="foreign_web", source_name="Korea", original_url=url),
    )
    discovery = _discovery(profile, [AdapterStatus(name="foreign_media", status="completed", candidate_count=1)], [cand])
    final = _result(source_type="foreign_web", url=url, title="Korea")
    final = explore.replace(final, payload=cand.payload)  # share the candidate id
    rows = reporting.compile_reporting_data(
        exploration_id="e1",
        discovery=discovery,
        fetched_articles=[final],
        source_window_issues=[{"item_url": url, "reason": "outside window", "source_name": "Korea"}],
        enriched_articles=[final],
        ranked_articles=[final],
        after_audit=[final],
        after_editorial=[final],
        after_critic=[final],
        final_results=[final],
        progress={},
    )
    row = next(r for r in rows if r["id"] == cand.payload.id)
    assert row["stages"]["recency"] is None  # included wins over recency
    assert row["stages"]["fetch"] is None


# ---------------------------------------------------------------------------
# Podcast: transcript-feed preference + transcription budget
# ---------------------------------------------------------------------------


def test_feed_transcript_url_prefers_plain_text() -> None:
    xml = (
        '<?xml version="1.0"?>'
        '<rss xmlns:podcast="https://podcastindex.org/namespace/1.0" version="2.0"><channel>'
        "<title>Show</title><item><title>Ep</title><description>notes</description>"
        '<enclosure url="https://cdn/ep.mp3" type="audio/mpeg" length="1"/>'
        '<podcast:transcript url="https://x/ep.html" type="text/html"/>'
        '<podcast:transcript url="https://x/ep.txt" type="text/plain"/>'
        "</item></channel></rss>"
    )
    eps = podcast.parse_podcast_feed(xml, feed_url="https://x/feed")
    assert eps[0].transcript_url == "https://x/ep.txt"


def test_episode_text_uses_feed_transcript(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(podcast, "_transcript_path", lambda episode: tmp_path / f"{episode.episode_id}.txt")

    async def fake_download(url: str) -> str:
        return "This is a long publisher transcript " * 20

    monkeypatch.setattr(podcast, "_download_transcript_text", fake_download)
    episode = podcast.PodcastEpisode(
        show_name="Show",
        feed_url="https://x/feed",
        episode_id="ep1",
        title="Ep",
        description="short notes",
        published_at=None,
        episode_url="https://x/ep",
        audio_url="https://cdn/ep.mp3",
        transcript_url="https://x/ep.txt",
    )
    text, source, _decisions, _metric = asyncio.run(podcast._episode_text(episode, {}, 0.9))
    assert source == "transcript_feed"
    assert "publisher transcript" in text


def test_episode_text_budget_blocks_audio_transcription(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(podcast, "_transcript_path", lambda episode: tmp_path / f"{episode.episode_id}.txt")
    monkeypatch.setattr(podcast, "_transcribe_command", lambda: "whisper {audio_path} {transcript_path}")

    def explode(*args, **kwargs):  # transcription must NOT run when disallowed
        raise AssertionError("transcription should be skipped under budget")

    monkeypatch.setattr(podcast, "_run_transcription", explode)
    episode = podcast.PodcastEpisode(
        show_name="Show",
        feed_url="https://x/feed",
        episode_id="ep2",
        title="Ep",
        description="useful show notes about AI infrastructure and HBM memory demand",
        published_at=None,
        episode_url="https://x/ep",
        audio_url="https://cdn/ep.mp3",
    )
    text, source, _decisions, _metric = asyncio.run(
        podcast._episode_text(episode, {}, 0.9, allow_transcription=False)
    )
    assert source == "show_notes"
    assert "show notes" in text


# ---------------------------------------------------------------------------
# Foreign media: English-gate exemption
# ---------------------------------------------------------------------------


def test_foreign_web_exempt_from_english_topic_gate() -> None:
    profile = _profile(
        statement="semiconductor memory HBM AI infrastructure capex",
        scope="semiconductor memory HBM AI infrastructure capex",
        keywords=["semiconductor", "memory", "hbm", "infrastructure", "capex"],
    )
    foreign = Candidate(
        adapter="foreign_media",
        score=0.6,
        payload=NormalizedPayload(
            source_type="foreign_web",
            source_name="banana orange mango apple",
            raw_text="banana orange mango apple",
            original_url="https://ex.kr/f",
            metadata={"source_language": "ko"},
        ),
    )
    web = Candidate(
        adapter="web_search",
        score=0.6,
        payload=NormalizedPayload(
            source_type="gmail_link",
            source_name="banana orange mango apple",
            raw_text="banana orange mango apple",
            original_url="https://ex.com/w",
            metadata={"search_query": "q"},
        ),
    )
    kept, _dropped = runner._apply_topic_relevance(profile, [foreign, web])
    kept_ids = {c.payload.id for c in kept}
    assert foreign.payload.id in kept_ids  # foreign exempt, kept despite no overlap
    assert web.payload.id not in kept_ids  # non-foreign with no overlap is dropped


# ---------------------------------------------------------------------------
# Reddit recency: undated/old posts no longer bypass the window
# ---------------------------------------------------------------------------


def test_reddit_post_is_strict_window_type() -> None:
    assert "reddit_post" in explore._STRICT_SOURCE_WINDOW_TYPES


def test_undated_reddit_post_excluded_by_window() -> None:
    profile = _profile(source_selection={"reddit": True})
    undated = _result(source_type="reddit_post", url="https://www.reddit.com/r/ai/comments/abc/")
    kept, issues, _reserve = explore._apply_source_window_filter(profile, [undated], lookback_hours=24)
    assert kept == []  # undated reddit no longer slips through the recency gate
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Podcast: freshest-in-window fallback when exact episode match fails
# ---------------------------------------------------------------------------


def test_latest_in_window_episode_picks_freshest_with_audio() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)

    def ep(eid: str, hours_ago: int, audio: str | None):
        return podcast.PodcastEpisode(
            show_name="Show",
            feed_url="https://x/feed",
            episode_id=eid,
            title=eid,
            description="d",
            published_at=(now - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
            episode_url=f"https://x/{eid}",
            audio_url=audio,
        )

    episodes = [
        ep("old", 400, "https://a/old.mp3"),
        ep("fresh", 5, "https://a/fresh.mp3"),
        ep("noaudio", 1, None),  # newest but unplayable -> skipped
    ]
    chosen = podcast._latest_in_window_episode(episodes, 168)
    assert chosen is not None and chosen.episode_id == "fresh"

    # Nothing inside the window -> None.
    assert podcast._latest_in_window_episode([ep("stale", 999, "https://a/s.mp3")], 24) is None
