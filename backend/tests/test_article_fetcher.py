from __future__ import annotations

import asyncio

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian import articles


ARTICLE_HTML = """
<!doctype html>
<html>
  <head>
    <meta property="og:title" content="Useful AI Article" />
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


class FakeAsyncClient:
    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url):
        return FakeResponse()


def test_extract_article_prefers_article_content():
    extracted = articles.extract_article(ARTICLE_HTML, "https://example.com/final-article")

    assert extracted.title == "Useful AI Article"
    assert "substantial paragraph about an AI system" in extracted.text
    assert "Subscribe now" not in extracted.text


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
    ]

    selected = articles.select_article_payloads(payloads)

    assert len(selected) == 1
    assert selected[0].original_url == "https://example.com/articles/useful-ai"
    assert selected[0].metadata["canonical_url"] == "https://example.com/articles/useful-ai"
