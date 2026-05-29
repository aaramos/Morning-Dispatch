from __future__ import annotations

import asyncio

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
