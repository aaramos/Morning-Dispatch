from __future__ import annotations

from dataclasses import replace

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.enrichment import enrich_article
from backend.app.db import database


def article_result() -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url="https://example.com/articles/agent-model",
        published_at="2026-05-20T12:00:00+00:00",
        metadata={"link_text": "Agent model improves developer workflows", "parent_subject": "AI newsletter"},
    )
    text = (
        "Agent model improves developer workflows. "
        "The new agent model improves developer workflows for product teams and helps automate coding tasks. "
        "It includes model evaluation, API tooling, and practical deployment controls for local AI infrastructure."
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url="https://example.com/articles/agent-model",
        canonical_url="https://example.com/articles/agent-model",
        title="Agent model improves developer workflows",
        text=text,
        excerpt=text[:240],
        domain="example.com",
        status="fetched",
        link_score=0.9,
    )


def test_model_enrichment_cache_round_trips(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    database.init_database()

    deterministic = enrich_article(article_result())
    model_enriched = replace(
        deterministic,
        title="Model cached title",
        excerpt="Model cached summary.",
        editor_summary="Model cached summary.",
        keywords=("model cache", "local ai"),
        content_type="article",
        enrichment_source="model",
    )

    cached_count = database.cache_model_enrichments([model_enriched], model_name="cache-test-model")
    fresh = enrich_article(article_result())
    cached_results = database.apply_cached_model_enrichments(
        [fresh],
        model_name="cache-test-model",
        limit=1,
    )

    assert cached_count == 1
    assert cached_results[0].title == "Model cached title"
    assert cached_results[0].editor_summary == "Model cached summary."
    assert cached_results[0].keywords == ("model cache", "local ai")
    assert cached_results[0].enrichment_source == "model_cache"
