from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, UTC, timedelta
from types import SimpleNamespace

import httpx

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery import google_news
from backend.agents.discovery.adapters import GoogleNewsSourceAdapter
from backend.agents.discovery.types import SourceAdapterContext, TopicProfile, Candidate
from backend.agents.editor import prepare_issue_articles
from backend.agents.librarian import articles
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.db import database


def _runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(tmp_path / "data" / "db" / "morning_dispatch.sqlite3"),
    )


# --- URL Builder Tests ---

def test_build_search_url_lookback_under_48h() -> None:
    url = google_news.build_search_url("artificial intelligence", lookback_hours=12)
    assert "q=artificial%20intelligence+when%3A12h" in url
    assert "hl=en-US" in url
    assert "gl=US" in url
    assert "ceid=US:en" in url


def test_build_search_url_lookback_under_30_days() -> None:
    url = google_news.build_search_url("agentic AI", lookback_hours=72)
    assert "q=agentic%20AI+when%3A3d" in url


def test_build_search_url_lookback_over_30_days() -> None:
    url = google_news.build_search_url("machine learning", lookback_hours=1000)
    expected_date = (datetime.now(UTC) - timedelta(hours=1000)).strftime("%Y-%m-%d")
    assert f"+after%3A{expected_date}" in url


def test_build_search_url_no_lookback() -> None:
    url = google_news.build_search_url("deep learning")
    assert "q=deep%20learning" in url
    assert "when" not in url
    assert "after" not in url


def test_build_search_url_custom_locale() -> None:
    url = google_news.build_search_url("deep learning", hl="pt-BR", gl="BR", ceid="BR:pt")
    assert "hl=pt-BR" in url
    assert "gl=BR" in url
    assert "ceid=BR:pt" in url


# --- RSS Parsing & Fetching Tests ---

def test_fetch_google_news_success(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    sample_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Google News - Search</title>
        <item>
          <title>Apple launches new CPU - TechCrunch</title>
          <link>https://news.google.com/rss/articles/CBMi_apple_cpu/</link>
          <description>&lt;p&gt;Apple announced their latest chips today.&lt;/p&gt;</description>
          <pubDate>Tue, 09 Jun 2026 10:30:00 GMT</pubDate>
          <source url="https://techcrunch.com">TechCrunch</source>
        </item>
      </channel>
    </rss>
    """

    class FakeResponse:
        def __init__(self, text: str, status_code: int = 200) -> None:
            self.text = text
            self.status_code = status_code

        def raise_for_status(self) -> None:
            pass

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def get(self, url: str, **kwargs) -> FakeResponse:
            return FakeResponse(sample_rss)

    monkeypatch.setattr(google_news, "shared_async_client", lambda **_kwargs: FakeAsyncClient())

    hits = asyncio.run(google_news.fetch_google_news("Apple CPU", limit=2))

    assert len(hits) == 1
    hit = hits[0]
    assert hit.title == "Apple launches new CPU"  # suffix stripped
    assert hit.url == "https://news.google.com/rss/articles/CBMi_apple_cpu/"
    assert hit.publisher == "TechCrunch"
    assert hit.snippet == "Apple announced their latest chips today."  # HTML tags stripped
    assert hit.published_at == "2026-06-09T10:30:00+00:00"


def test_fetch_google_news_retry_429(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    called_count = 0

    class FakeResponse:
        def __init__(self, text: str, status_code: int) -> None:
            self.text = text
            self.status_code = status_code

        def raise_for_status(self) -> None:
            pass

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def get(self, url: str, **kwargs) -> FakeResponse:
            nonlocal called_count
            called_count += 1
            if called_count == 1:
                return FakeResponse("", 429)
            return FakeResponse("""<?xml version="1.0" encoding="UTF-8"?><rss><channel><item><title>A</title><link>L</link></item></channel></rss>""", 200)

    monkeypatch.setattr(google_news, "shared_async_client", lambda **_kwargs: FakeAsyncClient())
    # speed up test by mocking sleep
    async def mock_sleep(seconds: float) -> None:
        pass
    monkeypatch.setattr(google_news.asyncio, "sleep", mock_sleep)

    hits = asyncio.run(google_news.fetch_google_news("retry test", limit=1))
    assert len(hits) == 1
    assert called_count == 2


# --- URL Decoding & Cache Tests ---

def test_extract_google_news_id() -> None:
    assert google_news.extract_google_news_id("https://news.google.com/articles/CBMi_test") == "CBMi_test"
    assert google_news.extract_google_news_id("https://news.google.com/rss/articles/CBMi_test?hl=en") == "CBMi_test"


def test_decode_google_news_url_caching(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    guid = "CBMi_cached_id"
    proxy_url = f"https://news.google.com/articles/{guid}"
    decoded_url = "https://original-publisher.com/article"

    # Seed the cache manually
    google_news._write_decode_cache(guid, decoded_url)

    # Calling decode should hit the cache immediately without making HTTP calls
    result = asyncio.run(google_news.decode_google_news_url(proxy_url))
    assert result == decoded_url


def test_decode_google_news_url_success(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    guid = "CBMi_real_id"
    proxy_url = f"https://news.google.com/articles/{guid}"
    target_url = "https://verified-publisher.com/story"

    redirect_html = """<html>
      <body>
        <div data-n-a-sg="sig-123" data-n-a-ts="1780000000" />
      </body>
    </html>"""

    batch_response_text = f'\n\n[[["wrb.fr", "Fbv4je", "[null, \\"{target_url}\\\"]"]]]'

    class FakeResponse:
        def __init__(self, text: str, status_code: int = 200) -> None:
            self.text = text
            self.status_code = status_code

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def get(self, url: str, **kwargs) -> FakeResponse:
            assert guid in url
            return FakeResponse(redirect_html)

        async def post(self, url: str, data: dict, **kwargs) -> FakeResponse:
            assert "batchexecute" in url
            assert "sig-123" in data["f.req"]
            return FakeResponse(batch_response_text)

    monkeypatch.setattr(google_news, "shared_async_client", lambda **_kwargs: FakeAsyncClient())

    decoded = asyncio.run(google_news.decode_google_news_url(proxy_url))
    assert decoded == target_url

    # Check that it wrote to the cache
    cached_val = google_news._read_decode_cache(guid)
    assert cached_val == target_url


def test_decode_google_news_url_sync_success(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    guid = "CBMi_sync_id"
    proxy_url = f"https://news.google.com/articles/{guid}"
    target_url = "https://verified-publisher-sync.com/story"

    redirect_html = """<html>
      <body>
        <div data-n-a-sg="sig-555" data-n-a-ts="1780000000" />
      </body>
    </html>"""

    batch_response_text = f'\n\n[[["wrb.fr", "Fbv4je", "[null, \\"{target_url}\\\"]"]]]'

    class FakeResponse:
        def __init__(self, text: str, status_code: int = 200) -> None:
            self.text = text
            self.status_code = status_code

    class FakeSyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> FakeSyncClient:
            return self

        def __exit__(self, *args) -> None:
            pass

        def get(self, url: str, **kwargs) -> FakeResponse:
            return FakeResponse(redirect_html)

        def post(self, url: str, data: dict, **kwargs) -> FakeResponse:
            return FakeResponse(batch_response_text)

    monkeypatch.setattr(google_news.httpx, "Client", FakeSyncClient)

    decoded = google_news.decode_google_news_url_sync(proxy_url)
    assert decoded == target_url


def test_decode_google_news_url_negative_cache_and_circuit_breaker(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    first_guid = "CBMi_blocked_one"
    second_guid = "CBMi_blocked_two"
    first_proxy = f"https://news.google.com/articles/{first_guid}"
    second_proxy = f"https://news.google.com/articles/{second_guid}"
    calls = {"get": 0}

    class FakeResponse:
        status_code = 429
        text = ""
        url = "https://www.google.com/sorry/index"

    class FakeAsyncClient:
        def __init__(self) -> None:
            self.cookies = httpx.Cookies()

        async def get(self, url: str, **kwargs) -> FakeResponse:
            calls["get"] += 1
            return FakeResponse()

    state = google_news.GoogleNewsDecodeState()
    client = FakeAsyncClient()

    assert asyncio.run(google_news.decode_google_news_url(first_proxy, client=client, state=state)) is None
    assert state.blocked is True
    assert state.reason == "decode_blocked"
    assert google_news.cached_google_news_decode_failure(first_proxy) == "decode_blocked"

    assert asyncio.run(google_news.decode_google_news_url(second_proxy, client=client, state=state)) is None
    assert calls["get"] == 1


# --- Adapter Integration Tests ---

def test_google_news_adapter_query(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    async def mock_fetch_news(query, **kwargs):
        return [
            google_news.GoogleNewsHit(
                title="Google News Integration works",
                url="https://news.google.com/rss/articles/CBMi_first/",
                decoded_url=None,
                snippet="A description of the project.",
                publisher="The Verge",
                published_at="2026-06-09T12:00:00+00:00"
            ),
            google_news.GoogleNewsHit(
                title="Google News Integration works",  # duplicate title
                url="https://news.google.com/rss/articles/CBMi_first_dup/",
                decoded_url=None,
                snippet="Duplicate description.",
                publisher="The Verge",
                published_at="2026-06-09T12:05:00+00:00"
            ),
            google_news.GoogleNewsHit(
                title="Unique article title",
                url="https://news.google.com/rss/articles/CBMi_second/",
                decoded_url=None,
                snippet="Another snippet content.",
                publisher="TechCrunch",
                published_at="2026-06-09T13:00:00+00:00"
            ),
            google_news.GoogleNewsHit(
                title="Third unique article",
                url="https://news.google.com/rss/articles/CBMi_third/",
                decoded_url=None,
                snippet="A third snippet.",
                publisher="Wired",
                published_at="2026-06-09T14:00:00+00:00"
            ),
        ]

    monkeypatch.setattr(GoogleNewsSourceAdapter, "query", GoogleNewsSourceAdapter.query)
    monkeypatch.setattr(google_news, "fetch_google_news", mock_fetch_news)

    # Mock decode URL to return original/decoded
    async def mock_decode(url, **kwargs):
        if "CBMi_first" in url:
            return "https://theverge.com/first-story"
        if "CBMi_second" in url:
            return "https://techcrunch.com/second-story"
        return url

    monkeypatch.setattr(google_news, "decode_google_news_url", mock_decode)

    adapter = GoogleNewsSourceAdapter()
    profile = TopicProfile.from_dict({
        "statement": "Morning Dispatch Improvements",
        "scope": "Google News RSS source adapter design.",
        "search_queries": ["morning dispatch improvement"],
    })
    context = SourceAdapterContext(exploration_id="explore-new-connector", candidate_limit=2)

    candidates = asyncio.run(adapter.query(profile, context))

    # Expecting 2 candidates after title deduplication
    assert len(candidates) == 2

    # Check details of the first candidate
    cand_1 = candidates[0]
    assert isinstance(cand_1, Candidate)
    assert cand_1.adapter == "google_news"
    assert cand_1.payload.source_type == "gmail_link"
    assert cand_1.payload.source_name == "The Verge"
    assert cand_1.payload.original_url == "https://theverge.com/first-story"
    assert cand_1.payload.published_at == "2026-06-09T12:00:00+00:00"
    assert cand_1.payload.metadata["search_provider"] == "google_news_rss"
    assert cand_1.payload.metadata["publisher"] == "The Verge"
    assert cand_1.payload.metadata["google_news_url"] == "https://news.google.com/rss/articles/CBMi_first/"
    assert cand_1.payload.metadata["title"] == "Google News Integration works"
    assert cand_1.payload.metadata["link_text"] == "Google News Integration works"
    assert cand_1.payload.metadata["google_news_resolution"] == "google_news_decode"

    # Check details of the second candidate
    cand_2 = candidates[1]
    assert cand_2.payload.source_name == "TechCrunch"
    assert cand_2.payload.original_url == "https://techcrunch.com/second-story"


def test_google_news_adapter_returns_partial_proxy_candidates_when_decode_times_out(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_GOOGLE_NEWS_REQUEST_DELAY_SECONDS", "0")
    monkeypatch.setenv("MORNING_DISPATCH_GOOGLE_NEWS_REQUEST_TIMEOUT_SECONDS", "0.5")

    async def mock_fetch_news(query, **kwargs):
        return [
            google_news.GoogleNewsHit(
                title="First recent article",
                url="https://news.google.com/rss/articles/CBMi_first/",
                decoded_url=None,
                snippet="A description of the first article.",
                publisher="The Verge",
                published_at="2026-06-09T12:00:00+00:00",
            ),
            google_news.GoogleNewsHit(
                title="Second recent article",
                url="https://news.google.com/rss/articles/CBMi_second/",
                decoded_url=None,
                snippet="A description of the second article.",
                publisher="TechCrunch",
                published_at="2026-06-09T13:00:00+00:00",
            ),
            google_news.GoogleNewsHit(
                title="Third recent article",
                url="https://news.google.com/rss/articles/CBMi_third/",
                decoded_url=None,
                snippet="A description of the third article.",
                publisher="Wired",
                published_at="2026-06-09T14:00:00+00:00",
            ),
        ]

    async def slow_decode(url, **kwargs):
        await asyncio.sleep(2)
        return "https://publisher.example.com/decoded"

    monkeypatch.setattr(google_news, "fetch_google_news", mock_fetch_news)
    monkeypatch.setattr(google_news, "decode_google_news_url", slow_decode)

    adapter = GoogleNewsSourceAdapter()
    profile = TopicProfile.from_dict(
        {
            "statement": "AI infrastructure news",
            "scope": "AI infrastructure news",
            "search_queries": ["ai infrastructure"],
        }
    )

    candidates = asyncio.run(adapter.query(profile, SourceAdapterContext(exploration_id="explore-google", candidate_limit=3)))

    assert len(candidates) == 3
    assert [candidate.payload.original_url for candidate in candidates] == [
        "https://news.google.com/rss/articles/CBMi_first/",
        "https://news.google.com/rss/articles/CBMi_second/",
        "https://news.google.com/rss/articles/CBMi_third/",
    ]
    assert all(candidate.payload.metadata["adapter_reason_code"] == "time_budget" for candidate in candidates)


def test_google_news_adapter_uses_serper_fallback_when_decode_fails(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_SERPER_API_KEY", "serper-test-key")

    async def mock_fetch_news(query, **kwargs):
        return [
            google_news.GoogleNewsHit(
                title="Nvidia AI infrastructure demand accelerates",
                url="https://news.google.com/rss/articles/CBMi_serper/",
                decoded_url=None,
                snippet="Nvidia AI infrastructure demand accelerates.",
                publisher="The Verge",
                published_at="2026-06-09T12:00:00+00:00",
            ),
            google_news.GoogleNewsHit(
                title="AMD AI infrastructure demand accelerates",
                url="https://news.google.com/rss/articles/CBMi_serper_2/",
                decoded_url=None,
                snippet="AMD AI infrastructure demand accelerates.",
                publisher="Reuters",
                published_at="2026-06-09T12:05:00+00:00",
            ),
            google_news.GoogleNewsHit(
                title="Memory supply chain demand accelerates",
                url="https://news.google.com/rss/articles/CBMi_serper_3/",
                decoded_url=None,
                snippet="Memory supply chain demand accelerates.",
                publisher="Bloomberg",
                published_at="2026-06-09T12:10:00+00:00",
            ),
        ]

    async def mock_decode(url, **kwargs):
        return None

    class FakeSerperBackend:
        def __init__(self, *, api_key: str, timeout_seconds: float) -> None:
            assert api_key == "serper-test-key"

        async def search(self, query: str, *, limit: int, days: int | None = None):
            assert '"Nvidia AI infrastructure demand accelerates" The Verge' == query
            return [
                SimpleNamespace(
                    url="https://www.theverge.com/2026/6/9/nvidia-ai-infrastructure",
                    title="Nvidia AI infrastructure demand accelerates",
                    snippet="The Verge reports on Nvidia AI infrastructure.",
                )
            ]

    monkeypatch.setattr(google_news, "fetch_google_news", mock_fetch_news)
    monkeypatch.setattr(google_news, "decode_google_news_url", mock_decode)
    monkeypatch.setattr("backend.agents.discovery.adapters.SerperBackend", FakeSerperBackend)

    adapter = GoogleNewsSourceAdapter()
    profile = TopicProfile.from_dict(
        {
            "statement": "AI infrastructure news",
            "scope": "AI infrastructure news",
            "search_queries": ["ai infrastructure"],
        }
    )

    candidates = asyncio.run(adapter.query(profile, SourceAdapterContext(exploration_id="explore-google", candidate_limit=1)))

    assert len(candidates) == 1
    assert candidates[0].payload.original_url == "https://www.theverge.com/2026/6/9/nvidia-ai-infrastructure"
    assert candidates[0].payload.metadata["google_news_resolution"] == "serper_fallback"
    assert "adapter_reason_code" not in candidates[0].payload.metadata


def test_article_selection_keeps_google_news_proxy_without_sync_decode(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    proxy_url = "https://news.google.com/rss/articles/CBMi_selected/"
    monkeypatch.setattr(
        google_news,
        "decode_google_news_url_sync",
        lambda _url: pytest.fail("article selection should not synchronously decode Google News proxy URLs"),
    )

    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="Google News",
        raw_text="A Google News result about AI infrastructure.",
        original_url=proxy_url,
        metadata={
            "search_provider": "google_news_rss",
            "google_news_url": proxy_url,
            "title": "AI infrastructure story from Google News",
            "link_text": "AI infrastructure story from Google News",
        },
    )

    selected = articles.select_article_payloads([payload], max_articles=1)

    assert len(selected) == 1
    assert selected[0].original_url == proxy_url.rstrip("/")
    assert selected[0].metadata["canonical_url"] == proxy_url.rstrip("/")
    assert selected[0].metadata["link_quality_score"] >= 0.55


def test_google_news_headline_only_result_survives_and_renders_as_news(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="The Verge",
        raw_text="AI infrastructure and HBM supply chain context from Google News.",
        original_url="https://news.google.com/rss/articles/CBMi_headline/",
        metadata={
            "search_provider": "google_news_rss",
            "google_news_url": "https://news.google.com/rss/articles/CBMi_headline/",
            "title": "AI infrastructure demand drives HBM supply chain investment",
            "link_text": "AI infrastructure demand drives HBM supply chain investment",
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url=str(payload.original_url),
        canonical_url=str(payload.original_url),
        title="AI infrastructure demand drives HBM supply chain investment",
        text="",
        excerpt="Google News snippet about AI infrastructure, HBM memory, supply chain investment, and chip capacity.",
        domain="news.google.com",
        status="no_content",
        error="Readable article text was too short (0 chars)",
        link_score=0.55,
        metadata={},
    )

    prepared = prepare_issue_articles(
        {"interest": "AI infrastructure HBM memory supply chain", "threshold": 0.45},
        [result],
    )

    assert len(prepared) == 1
    assert prepared[0].tier == "lower_confidence"
    assert prepared[0].section == "News"

    html = database.render_ingested_issue(
        "Google News Brief",
        "Snapshot",
        [payload],
        prepared,
        lookback_hours=24,
        source_selection={"google_news": True},
    )

    assert "Worth a skim" in html
    assert "AI infrastructure demand drives HBM supply chain investment" in html
    assert ">News</span>" in html


def test_google_news_remainder_renders_in_dedicated_news_section(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    payloads: list[NormalizedPayload] = []
    results: list[ArticleFetchResult] = []
    for index in range(6):
        payload = NormalizedPayload(
            source_type="gmail_link",
            source_name="Reuters",
            raw_text=f"AI infrastructure news item {index}",
            original_url=f"https://publisher.example.com/news-{index}",
            metadata={
                "search_provider": "google_news_rss",
                "title": f"AI infrastructure news item {index}",
                "link_text": f"AI infrastructure news item {index}",
            },
        )
        payloads.append(payload)
        results.append(
            ArticleFetchResult(
                payload=payload,
                original_url=str(payload.original_url),
                final_url=str(payload.original_url),
                canonical_url=str(payload.original_url),
                title=f"AI infrastructure news item {index}",
                text="AI infrastructure coverage about chips, models, and investment signals.",
                excerpt="AI infrastructure coverage about chips, models, and investment signals.",
                domain="publisher.example.com",
                status="fetched",
                link_score=0.8 - (index * 0.01),
                relevance_score=0.8 - (index * 0.01),
                tier="main",
                section="News",
            )
        )

    html = database.render_ingested_issue(
        "Google News Brief",
        "Snapshot",
        payloads,
        results,
        lookback_hours=24,
        source_selection={"google_news": True},
    )

    assert '<h2 id="source-news-heading">News</h2>' in html
