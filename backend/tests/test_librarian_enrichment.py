from __future__ import annotations

import asyncio
from dataclasses import replace

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.enrichment import (
    enrich_article,
    enrich_article_with_model,
    enrich_articles,
    refine_ranked_articles_with_model,
)
from backend.agents.model import ModelClientError, ModelResponse
from backend.app.db import database


def article_result(
    *,
    title: str = "Agent model improves developer workflows",
    text: str = (
        "Agent model improves developer workflows. "
        "The new agent model improves developer workflows for product teams and helps automate coding tasks. "
        "It includes model evaluation, API tooling, and practical deployment controls for local AI infrastructure."
    ),
    final_url: str = "https://example.com/articles/agent-model",
    status: str = "fetched",
    error: str | None = None,
) -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url="https://example.com/articles/agent-model",
        published_at="2026-05-20T12:00:00+00:00",
        metadata={"link_text": title, "parent_subject": "AI newsletter"},
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url=final_url,
        canonical_url=final_url,
        title=title,
        text=text,
        excerpt=text[:240],
        domain="example.com",
        status=status,
        error=error,
        link_score=0.9,
    )


def test_librarian_adds_summary_keywords_and_content_type():
    enriched = enrich_article(article_result())

    assert enriched.editor_summary
    assert "agent" in enriched.keywords
    assert enriched.content_type == "article"
    assert not enriched.text.startswith(enriched.title)


def test_librarian_marks_unresolved_links_as_fallback_snippets():
    enriched = enrich_article(
        article_result(
            text="",
            status="http_error",
            error="HTTP 403",
        )
    )

    assert enriched.content_type == "fallback_snippet"
    assert "could not be fully read" in enriched.editor_summary


def test_librarian_drops_author_bio_pages():
    author_page = article_result(
        title="Sabrina Ortiz",
        text=(
            "Sabrina Ortiz is a Senior Reporter at The Deep View. Previously, Sabrina led AI coverage. "
            "Google remakes Search with AI once again and adds more agent tools."
        ),
        final_url="https://www.thedeepview.com/author/sabrina-ortiz",
    )

    assert asyncio.run(enrich_articles([author_page])) == []


def test_librarian_uses_model_enrichment_when_available():
    class FakeModelClient:
        async def complete_json(self, **_kwargs):
            return {
                "title": "Canonical Agent Workflow Analysis",
                "summary": "This article explains how agent models change product workflows and developer automation.",
                "keywords": ["agent models", "developer tools", "local AI"],
                "content_type": "analysis",
            }

    enriched = asyncio.run(enrich_article_with_model(article_result(), model_client=FakeModelClient()))

    assert enriched.title == "Canonical Agent Workflow Analysis"
    assert enriched.editor_summary.startswith("This article explains")
    assert enriched.keywords == ("agent models", "developer tools", "local AI")
    assert enriched.content_type == "article"
    assert enriched.enrichment_source == "model"


def test_librarian_falls_back_when_model_enrichment_fails():
    class FailingModelClient:
        async def complete_json(self, **_kwargs):
            raise ModelClientError("offline")

    enriched = asyncio.run(enrich_article_with_model(article_result(), model_client=FailingModelClient()))

    assert enriched.title == "Agent model improves developer workflows"
    assert enriched.editor_summary


def test_librarian_limits_model_enrichment_to_top_candidates():
    class CountingModelClient:
        def __init__(self):
            self.calls = 0

        async def complete_json(self, **_kwargs):
            self.calls += 1
            return {
                "title": "Model enriched article",
                "summary": "The model enriched the highest quality article candidate.",
                "keywords": ["model enriched"],
                "content_type": "article",
            }

    client = CountingModelClient()
    results = [
        replace(article_result(title="Low score article"), link_score=0.1),
        replace(
            article_result(title="High score article", final_url="https://example.com/articles/high-score"),
            link_score=0.9,
        ),
    ]

    enriched = asyncio.run(enrich_articles(results, model_client=client, model_max_items=1))

    assert client.calls == 1
    assert [result.title for result in enriched] == ["Low score article", "Model enriched article"]


def test_librarian_refines_ranked_articles_in_display_order():
    class CountingModelClient:
        def __init__(self):
            self.calls = 0

        async def complete_json(self, **_kwargs):
            self.calls += 1
            return {
                "title": f"Model enriched article {self.calls}",
                "summary": "The model refined the ranked article.",
                "keywords": ["ranked model"],
                "content_type": "article",
            }

    client = CountingModelClient()
    results = [
        replace(article_result(title="Lead ranked article"), link_score=0.1, tier="lead"),
        replace(article_result(title="Second ranked article"), link_score=0.9, tier="main"),
    ]

    enriched = asyncio.run(refine_ranked_articles_with_model(results, model_client=client, model_max_items=1))

    assert client.calls == 1
    assert [result.title for result in enriched] == ["Model enriched article 1", "Second ranked article"]


def test_librarian_skips_cached_model_enrichment():
    class CountingModelClient:
        def __init__(self):
            self.calls = 0

        async def complete_json(self, **_kwargs):
            self.calls += 1
            return {
                "title": "Unexpected model call",
                "summary": "The model should not run for cached article metadata.",
                "keywords": ["unexpected"],
                "content_type": "article",
            }

    client = CountingModelClient()
    cached = replace(
        article_result(title="Cached model article"),
        editor_summary="Cached summary from the previous model run.",
        excerpt="Cached summary from the previous model run.",
        keywords=("cached", "summary"),
        enrichment_source="model_cache",
    )

    enriched = asyncio.run(refine_ranked_articles_with_model([cached], model_client=client, model_max_items=1))

    assert client.calls == 0
    assert enriched[0].title == "Cached model article"
    assert enriched[0].editor_summary == "Cached summary from the previous model run."
    assert enriched[0].enrichment_source == "model_cache"


def test_librarian_records_inference_metric(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    database.init_database()

    class Config:
        model = "Gemma4-27B-Q6"
        base_url = "http://127.0.0.1:1234/v1"

    class MetricsModelClient:
        config = Config()

        async def complete_json_with_metrics(self, **_kwargs):
            return (
                ModelResponse(
                    content="{}",
                    queue_wait_ms=12,
                    ttft_ms=None,
                    generation_ms=None,
                    total_ms=2450,
                    prompt_tokens=550,
                    completion_tokens=80,
                    tokens_per_sec=None,
                ),
                {
                    "title": "Measured model article",
                    "summary": "The model summary is measured and cached for later comparison.",
                    "keywords": ["metrics"],
                    "content_type": "article",
                    "confidence": 0.82,
                },
            )

    enriched = asyncio.run(
        enrich_article_with_model(
            article_result(),
            model_client=MetricsModelClient(),
            metrics_context={"run_id": "run-1", "article_id": "article-1", "mode": "batch"},
        )
    )
    summary = database.inference_metrics_summary()

    assert enriched.enrichment_source == "model"
    assert summary["record_count"] == 1
    assert summary["success_count"] == 1
    assert summary["models"][0]["model"] == "Gemma4-27B-Q6"
    assert summary["models"][0]["quantization"] == "Q6"
    assert summary["models"][0]["p95_total_ms"] == 2438
    assert summary["models"][0]["avg_queue_wait_ms"] == 12.0
