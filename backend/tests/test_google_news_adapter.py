from __future__ import annotations

import asyncio
import hashlib
import json
import time
import pytest
from datetime import datetime, UTC, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from bs4 import BeautifulSoup

from backend.agents.discovery import google_news
from backend.agents.discovery.adapters import GoogleNewsSourceAdapter
from backend.agents.discovery.types import SourceAdapterContext, TopicProfile, Candidate
from backend.app.core.config import get_settings


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

    monkeypatch.setattr(google_news.httpx, "AsyncClient", FakeAsyncClient)

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

    monkeypatch.setattr(google_news.httpx, "AsyncClient", FakeAsyncClient)
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

    redirect_html = f"""<html>
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

    monkeypatch.setattr(google_news.httpx, "AsyncClient", FakeAsyncClient)

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

    redirect_html = f"""<html>
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


# --- Adapter Integration Tests ---

def test_google_news_adapter_query(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)

    # Mock fetch_google_news_sequential to return sample hits
    async def mock_fetch_seq(queries, **kwargs):
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
            )
        ]

    monkeypatch.setattr(GoogleNewsSourceAdapter, "query", GoogleNewsSourceAdapter.query)
    monkeypatch.setattr(google_news, "fetch_google_news_sequential", mock_seq_helper := mock_fetch_seq)

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
    context = SourceAdapterContext(exploration_id="explore-new-connector", candidate_limit=5)

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

    # Check details of the second candidate
    cand_2 = candidates[1]
    assert cand_2.payload.source_name == "TechCrunch"
    assert cand_2.payload.original_url == "https://techcrunch.com/second-story"
