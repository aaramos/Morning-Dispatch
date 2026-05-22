from __future__ import annotations

import asyncio

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.editor import build_issue_snapshot, prepare_issue_articles
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.enrichment import enrich_article, enrich_articles


def result(title: str, text: str, *, link_score: float = 0.9) -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url=f"https://example.com/articles/{title.lower().replace(' ', '-')}",
        published_at="2026-05-20T12:00:00+00:00",
        metadata={"link_text": title, "parent_subject": "AI newsletter"},
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url=str(payload.original_url),
        canonical_url=str(payload.original_url),
        title=title,
        text=text,
        excerpt=text[:240],
        domain="example.com",
        status="fetched",
        link_score=link_score,
    )


def test_prepare_issue_articles_scores_summarizes_and_marks_lead():
    digest = {
        "interest": "Local AI infrastructure, model releases, agent tools, and product strategy",
        "threshold": 0.45,
    }
    prepared = prepare_issue_articles(
        digest,
        asyncio.run(
            enrich_articles(
                [
                    result(
                        "New agent model improves local AI workflows",
                        (
                            "The new agent model improves local AI workflows for product teams. "
                            "It reduces inference cost and lets operators run model evaluation on local infrastructure. "
                            "The release matters because developer tools can now automate longer product workflows."
                        ),
                    ),
                    result(
                        "Unrelated sports roundup",
                        "The local team won a game with a late goal. Fans celebrated around the city.",
                        link_score=0.2,
                    ),
                ]
            )
        ),
    )

    assert len(prepared) == 1
    assert prepared[0].tier == "lead"
    assert prepared[0].relevance_score and prepared[0].relevance_score >= 0.45
    assert "local AI workflows" in prepared[0].editor_summary
    assert prepared[0].section in {"Models & Labs", "Agents & Developer Tools", "AI Infrastructure"}


def test_build_issue_snapshot_uses_ranked_articles():
    prepared = prepare_issue_articles(
        {"interest": "AI model releases", "threshold": 0.45},
        asyncio.run(
            enrich_articles(
                [
                    result(
                        "Gemini model release",
                        "Google released a Gemini model for AI agents. The model improves coding and workflow automation.",
                    )
                ]
            )
        ),
    )

    snapshot = build_issue_snapshot(1, 1, prepared)

    assert "Gemini model release" in snapshot
    assert "ranked article" in snapshot


def test_prepare_issue_articles_drops_author_bios_after_redirect():
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url="https://example.com/redirect",
        published_at="2026-05-20T12:00:00+00:00",
        metadata={"link_text": "Google remakes Search with AI"},
    )
    author_page = ArticleFetchResult(
        payload=payload,
        original_url="https://example.com/redirect",
        final_url="https://www.thedeepview.com/author/sabrina-ortiz",
        canonical_url="https://www.thedeepview.com/author/sabrina-ortiz",
        title="Sabrina Ortiz",
        text=(
            "Sabrina Ortiz is a Senior Reporter at The Deep View. Previously, Sabrina led AI coverage. "
            "Google remakes Search with AI once again and adds more agent tools."
        ),
        excerpt="Sabrina Ortiz is a Senior Reporter at The Deep View.",
        domain="thedeepview.com",
        status="fetched",
        link_score=0.9,
    )

    prepared = prepare_issue_articles({"interest": "AI model release news", "threshold": 0.45}, [enrich_article(author_page)])

    assert prepared == []
