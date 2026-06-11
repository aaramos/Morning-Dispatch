from __future__ import annotations

import asyncio

import pytest

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian import articles


ARTICLE_HTML = """
<!doctype html>
<html>
  <head>
    <meta property="og:title" content="Useful AI Article" />
    <meta property="og:image" content="/images/useful-ai.jpg" />
  </head>
  <body>
    <nav>Subscribe now</nav>
    <article>
      <h1>Useful AI Article</h1>
      <p>This is a substantial paragraph about an AI system and how it changes product workflows for operators.</p>
      <p>Another substantial paragraph explains the deployment details, customer impact, and technical constraints in practical language.</p>
      <p>The final substantial paragraph provides enough detail for extraction to treat this page as a readable article.</p>
      <p>Additional context makes the article long enough to pass the readable text threshold for the fetcher.</p>
      <p>More context covers examples, caveats, and implementation notes for a product manager audience.</p>
    </article>
  </body>
</html>
"""


class FakeResponse:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}
    text = ARTICLE_HTML
    url = "https://example.com/final-article"


class ForbiddenResponse:
    status_code = 403
    headers = {"content-type": "text/html; charset=utf-8"}
    text = "<html><body>Forbidden</body></html>"
    url = "https://example.com/paywalled"


class ShortResponse:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}
    text = "<html><body><article><h1>Short AI item</h1><p>Too short to be considered a readable article.</p></article></body></html>"
    url = "https://example.com/short"


class FakeAsyncClient:
    response = FakeResponse()

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url):
        return self.response


def test_extract_article_prefers_article_content():
    extracted = articles.extract_article(ARTICLE_HTML, "https://example.com/final-article")

    assert extracted.title == "Useful AI Article"
    assert "substantial paragraph about an AI system" in extracted.text
    assert "Subscribe now" not in extracted.text
    assert extracted.image_url == "https://example.com/images/useful-ai.jpg"
    assert extracted.image_source == "og:image"


def test_extract_article_harvests_published_date_from_meta():
    html = ARTICLE_HTML.replace(
        '<meta property="og:image" content="/images/useful-ai.jpg" />',
        '<meta property="og:image" content="/images/useful-ai.jpg" />'
        '<meta property="article:published_time" content="2026-05-20T08:00:00Z" />',
    )
    extracted = articles.extract_article(html, "https://example.com/final-article")
    assert extracted.published_at == "2026-05-20T08:00:00Z"


def test_extract_article_harvests_published_date_from_jsonld():
    html = ARTICLE_HTML.replace(
        "</head>",
        '<script type="application/ld+json">'
        '{"@type":"NewsArticle","datePublished":"2026-04-01T10:00:00+00:00"}'
        "</script></head>",
    )
    extracted = articles.extract_article(html, "https://example.com/final-article")
    assert extracted.published_at == "2026-04-01T10:00:00+00:00"


def test_extract_article_harvests_published_date_from_byline_element():
    html = ARTICLE_HTML.replace(
        "<h1>Useful AI Article</h1>",
        '<span class="entry-date updated">May 04, 2026</span><h1>Useful AI Article</h1>',
    )
    extracted = articles.extract_article(html, "https://example.com/final-article")
    assert extracted.published_at == "2026-05-04"


def test_extract_article_harvests_published_date_from_visible_header_text():
    html = ARTICLE_HTML.replace(
        "<h1>Useful AI Article</h1>",
        "<h1>Useful AI Article</h1><div>2026-01-29 · 22 mins · Written by Jason Huang</div>",
    )
    extracted = articles.extract_article(html, "https://example.com/final-article")
    assert extracted.published_at == "2026-01-29"


def test_extract_article_handles_js_date_string_meta():
    html = ARTICLE_HTML.replace(
        "</head>",
        '<meta property="pagefind:date" '
        'content="Thu Mar 12 2026 00:00:00 GMT+0000 (Coordinated Universal Time)" /></head>',
    )
    extracted = articles.extract_article(html, "https://example.com/final-article")
    assert "Mar 12 2026" in (extracted.published_at or "")


def test_normalize_date_text_formats():
    assert articles._normalize_date_text("April 23, 2026") == "2026-04-23"
    assert articles._normalize_date_text("30 April 2026") == "2026-04-30"
    assert articles._normalize_date_text("2026/04/20") == "2026-04-20"
    assert articles._normalize_date_text("Published 2026-01-27 by staff") == "2026-01-27"
    assert articles._normalize_date_text("no date here") is None
    assert articles._normalize_date_text("2026/01/50") is None  # invalid day rejected


def test_extract_article_published_date_absent_when_undated():
    extracted = articles.extract_article(ARTICLE_HTML, "https://example.com/final-article")
    assert extracted.published_at is None


def test_fetch_articles_backfills_published_at_from_page(monkeypatch):
    class DatedResponse(FakeResponse):
        text = ARTICLE_HTML.replace(
            "</head>",
            '<meta property="article:published_time" content="2026-05-20T08:00:00Z" /></head>',
        )

    class DatedClient(FakeAsyncClient):
        response = DatedResponse()

    monkeypatch.setattr(articles.httpx, "AsyncClient", DatedClient)
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="web search hit",
        original_url="https://example.com/article",
    )

    results = asyncio.run(articles.fetch_articles_for_payloads([payload]))

    assert results[0].payload.published_at == "2026-05-20T08:00:00Z"


def test_fetch_articles_prefers_page_published_date_over_provider_date(monkeypatch):
    class DatedResponse(FakeResponse):
        text = ARTICLE_HTML.replace(
            "<h1>Useful AI Article</h1>",
            "<h1>Useful AI Article</h1><div>2026-01-29 · 22 mins · Written by Jason Huang</div>",
        )

    class DatedClient(FakeAsyncClient):
        response = DatedResponse()

    monkeypatch.setattr(articles.httpx, "AsyncClient", DatedClient)
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="web search hit",
        original_url="https://example.com/article",
        published_at="2026-05-29",
    )

    results = asyncio.run(articles.fetch_articles_for_payloads([payload]))

    assert results[0].payload.published_at == "2026-01-29"


def test_fetch_cache_reuses_body_and_force_refresh_bypasses(monkeypatch, tmp_path):
    monkeypatch.setattr(articles, "_fetch_cache_dir", lambda: tmp_path)
    calls = {"n": 0}

    class CountingClient:
        async def get(self, _url):
            calls["n"] += 1
            return FakeResponse()

    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="web hit",
        original_url="https://example.com/cache-me",
        metadata={"link_quality_score": 0.9},
    )

    async def run():
        sem = asyncio.Semaphore(2)
        client = CountingClient()
        miss = await articles._fetch_one(client, sem, payload, cache_ttl=3600, force_refresh=False)
        hit = await articles._fetch_one(client, sem, payload, cache_ttl=3600, force_refresh=False)
        forced = await articles._fetch_one(client, sem, payload, cache_ttl=3600, force_refresh=True)
        return miss, hit, forced

    miss, hit, forced = asyncio.run(run())
    assert calls["n"] == 2  # miss fetched, hit served from cache, forced re-fetched
    assert miss.status == hit.status == "fetched"
    assert hit.title == miss.title  # re-extracted from cached HTML, identical content


def test_fetch_cache_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(articles, "_fetch_cache_dir", lambda: tmp_path)
    calls = {"n": 0}

    class CountingClient:
        async def get(self, _url):
            calls["n"] += 1
            return FakeResponse()

    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="web hit",
        original_url="https://example.com/no-cache",
        metadata={"link_quality_score": 0.9},
    )

    async def run():
        sem = asyncio.Semaphore(2)
        client = CountingClient()
        await articles._fetch_one(client, sem, payload, cache_ttl=0)
        await articles._fetch_one(client, sem, payload, cache_ttl=0)

    asyncio.run(run())
    assert calls["n"] == 2  # ttl=0 => every call hits the network


def test_fetch_articles_resolves_and_extracts(monkeypatch):
    monkeypatch.setattr(articles.httpx, "AsyncClient", FakeAsyncClient)
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url="https://newsletter.example.com/redirect",
        metadata={"link_text": "Useful AI Article"},
    )

    results = asyncio.run(articles.fetch_articles_for_payloads([payload]))

    assert len(results) == 1
    assert results[0].status == "fetched"
    assert results[0].final_url == "https://example.com/final-article"
    assert results[0].canonical_url == "https://example.com/final-article"
    assert results[0].domain == "example.com"
    assert results[0].metadata["image_url"] == "https://example.com/images/useful-ai.jpg"
    assert results[0].metadata["image_source"] == "og:image"


def test_select_article_payloads_filters_junk_and_deduplicates():
    payloads = [
        NormalizedPayload(
            source_type="gmail_link",
            original_url="https://example.com/articles/useful-ai?utm_source=newsletter&_bhlid=abc",
            metadata={"link_text": "Useful AI article"},
        ),
        NormalizedPayload(
            source_type="gmail_link",
            original_url="https://example.com/articles/useful-ai?utm_campaign=dupe",
            metadata={"link_text": "Useful AI article"},
        ),
        NormalizedPayload(
            source_type="gmail_link",
            original_url="https://example.com/unsubscribe",
            metadata={"link_text": "Unsubscribe"},
        ),
        NormalizedPayload(
            source_type="gmail_link",
            original_url="https://x.com/example/status/1",
            metadata={"link_text": "Share on X"},
        ),
        NormalizedPayload(
            source_type="gmail_link",
            original_url="https://jobs.ashbyhq.com/openai/example",
            metadata={"link_text": "Researcher, Models"},
        ),
        NormalizedPayload(
            source_type="gmail_link",
            original_url="https://link.mail.beehiiv.com/ss/c/u001.example",
            metadata={"link_text": "Trending AI Tools"},
        ),
        NormalizedPayload(
            source_type="gmail_link",
            original_url="https://link.mail.beehiiv.com/ss/c/u002.example",
            metadata={"link_text": "Community AI workflows"},
        ),
    ]

    selected = articles.select_article_payloads(payloads)

    assert len(selected) == 1
    assert selected[0].original_url == "https://example.com/articles/useful-ai"
    assert selected[0].metadata["canonical_url"] == "https://example.com/articles/useful-ai"


def test_fetch_articles_classifies_blocked_and_keeps_newsletter_context(monkeypatch):
    class BlockedClient(FakeAsyncClient):
        response = ForbiddenResponse()

    monkeypatch.setattr(articles.httpx, "AsyncClient", BlockedClient)
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url="https://example.com/paywalled",
        raw_text="Newsletter context says this paywalled story covers agentic AI product workflows.",
        metadata={"link_text": "Paywalled agent story", "parent_subject": "Agentic AI updates"},
    )

    results = asyncio.run(articles.fetch_articles_for_payloads([payload]))

    assert results[0].status == "blocked"
    assert results[0].error == "HTTP 403"
    assert "paywalled story covers agentic AI" in results[0].excerpt


def test_fetch_articles_uses_newsletter_context_for_short_pages(monkeypatch):
    class ShortClient(FakeAsyncClient):
        response = ShortResponse()

    monkeypatch.setattr(articles.httpx, "AsyncClient", ShortClient)
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url="https://example.com/short",
        raw_text="Newsletter context explains that this link is about local model benchmarks and faster agent workflows.",
        metadata={"link_text": "Short AI item", "parent_subject": "Local model benchmarks"},
    )

    results = asyncio.run(articles.fetch_articles_for_payloads([payload]))

    assert results[0].status == "no_content"
    assert "local model benchmarks" in results[0].excerpt
    assert "short" in results[0].error


def test_fetch_articles_promotes_short_pages_with_substantial_newsletter_context(monkeypatch):
    class ShortClient(FakeAsyncClient):
        response = ShortResponse()

    monkeypatch.setattr(articles.httpx, "AsyncClient", ShortClient)
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url="https://example.com/short",
        raw_text=(
            "Google launches managed agents with Linux environments for developers building AI workflows. "
            "The newsletter explains that teams can deploy an agent with one API call, skip sandbox setup, "
            "and move from mobile prototyping to production infrastructure faster."
        ),
        metadata={"link_text": "Google managed agents", "parent_subject": "Agent infrastructure"},
    )

    results = asyncio.run(articles.fetch_articles_for_payloads([payload]))

    assert results[0].status == "fetched"
    assert results[0].enrichment_source == "newsletter_context"
    assert "managed agents with Linux environments" in results[0].excerpt


def test_select_article_payloads_unwraps_redirect_links():
    payloads = [
        NormalizedPayload(
            source_type="gmail_link",
            original_url=(
                "https://newsletter.example.com/redirect?"
                "url=https%3A%2F%2Fexample.com%2Farticles%2Fuseful-ai%3Futm_source%3Dmail"
            ),
            metadata={"link_text": "Useful AI article"},
        )
    ]

    selected = articles.select_article_payloads(payloads)

    assert selected[0].original_url == "https://example.com/articles/useful-ai"


def test_score_link_candidate_newsletter_redirect():
    # A standard tracking link that would normally get scored very poorly (e.g. 0.12 or less)
    # should get scored at least 0.55 if it's on a known redirect / tracking domain.
    score_normal = articles.score_link_candidate("https://clicks.substack.com/f/a/some-tracking-hash", "generic text")
    assert score_normal >= 0.55

    score_custom = articles.score_link_candidate("https://link.mail.beehiiv.com/ss/c/another-hash", "another generic text")
    assert score_custom >= 0.55


@pytest.mark.parametrize(
    ("source_type", "section", "content_type", "default_score"),
    [
        ("gmail", "Newsletter Content", "newsletter", 0.80),
        ("reddit_thread", "Legacy Discussion", "reddit_thread", 0.65),
        ("reddit_post", "Legacy Discussion", "reddit_thread", 0.65),
        ("podcast_episode", "Podcast Signals", "podcast", 0.65),
        ("youtube_video", "YouTube Videos", "video", 0.65),
        ("collection_chunk", "Collections", "collection", 0.65),
        ("market_snapshot", "Markets", "market", 0.65),
        ("sec_filing", "SEC Filings", "sec_filing", 0.85),
        ("fred_series", "Macro Indicators", "fred_series", 0.88),
    ],
)
def test_direct_article_results_source_table(source_type, section, content_type, default_score):
    payload = NormalizedPayload(
        source_type=source_type,
        source_name="direct source",
        original_url="https://example.com/direct-item",
        raw_text="Direct payload body text for table-driven mapping checks.",
    )

    results = articles.direct_article_results([payload])

    assert len(results) == 1
    result = results[0]
    assert result.status == "fetched"
    assert result.section == section
    assert result.content_type == content_type
    assert result.link_score == pytest.approx(default_score)


def test_direct_article_results_table_covers_every_mapped_source_type():
    table_types = set(articles._DIRECT_SOURCE_META)
    assert table_types == {
        "gmail",
        "reddit_thread",
        "reddit_post",
        "podcast_episode",
        "youtube_video",
        "collection_chunk",
        "market_snapshot",
        "sec_filing",
        "fred_series",
    }


def test_direct_article_results_prefers_quality_score_metadata():
    payload = NormalizedPayload(
        source_type="podcast_episode",
        source_name="podcast feed",
        original_url="https://example.com/episode",
        raw_text="Episode summary text.",
        metadata={"episode_quality_score": 0.91},
    )

    results = articles.direct_article_results([payload])

    assert results[0].link_score == pytest.approx(0.91)


def test_direct_article_results_quality_chain_accepts_any_known_key():
    # Behavior preserved from the pre-table implementation: the score chain tries
    # every known quality key regardless of source_type.
    payload = NormalizedPayload(
        source_type="gmail",
        source_name="newsletter@example.com",
        original_url="https://example.com/newsletter",
        raw_text="Newsletter body.",
        metadata={"thread_quality_score": 0.42},
    )

    results = articles.direct_article_results([payload])

    assert results[0].link_score == pytest.approx(0.42)


def test_direct_article_results_skips_unmapped_or_urlless_payloads():
    unmapped = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url="https://example.com/article",
        raw_text="Link payload handled by the fetch path instead.",
    )
    urlless = NormalizedPayload(
        source_type="gmail",
        source_name="newsletter@example.com",
        original_url="",
        raw_text="Direct payload without a URL.",
    )

    assert articles.direct_article_results([unmapped, urlless]) == []
