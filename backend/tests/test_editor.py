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


def test_prepare_issue_articles_keeps_reddit_community_signals():
    payload = NormalizedPayload(
        source_type="reddit_thread",
        source_name="r/ollama",
        raw_text="Builders compare local LLM coding agents, MCP tools, and day-to-day workflow reliability.",
        original_url="https://reddit.com/r/ollama/comments/thread-1/local_agents/",
        published_at="2026-05-22T12:00:00+00:00",
        metadata={"title": "What local model are you actually using for coding tasks?"},
    )
    reddit_result = ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url=str(payload.original_url),
        canonical_url=str(payload.original_url),
        title="What local model are you actually using for coding tasks?",
        text=payload.raw_text,
        excerpt=payload.raw_text,
        domain="reddit.com",
        status="fetched",
        link_score=0.42,
        content_type="discussion",
    )

    prepared = prepare_issue_articles(
        {"interest": "Local LLM coding agents and product workflows", "threshold": 0.45},
        [enrich_article(reddit_result)],
    )

    assert len(prepared) == 1
    assert prepared[0].section == "Community Signals"
    assert prepared[0].tier == "lead"


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


def test_prepare_issue_articles_drops_lifestyle_story_from_ai_newsletter():
    prepared = prepare_issue_articles(
        {"interest": "Local AI and model release news", "threshold": 0.45},
        [
            enrich_article(
                result(
                    "How to Grow and Care for Dog Vomit Slime Mold",
                    (
                        "Dog vomit slime mold is a harmless protist that thrives on decaying organic matter "
                        "in moist garden mulch. Gardeners can remove it by scooping affected mulch or waiting "
                        "for natural predators like slugs and beetles."
                    ),
                    link_score=0.98,
                )
            )
        ],
    )

    assert prepared == []


def test_prepare_issue_articles_drops_incidental_ai_mention():
    prepared = prepare_issue_articles(
        {"interest": "Local AI and model release news", "threshold": 0.45},
        [
            enrich_article(
                result(
                    "Spotify to Reserve Concert Tickets for Top Fans",
                    (
                        "Spotify will reserve concert tickets for highly engaged listeners. "
                        "The platform will monitor streams and shares to prevent bots or AI agents "
                        "from gaming access to tickets, but the story is about music fandom and ticketing."
                    ),
                    link_score=0.95,
                )
            )
        ],
    )

    assert prepared == []


def test_prepare_issue_articles_keeps_clear_ai_product_story():
    prepared = prepare_issue_articles(
        {"interest": "Local AI and model release news", "threshold": 0.45},
        [
            enrich_article(
                result(
                    "Spotify launches AI audiobook creation tools",
                    (
                        "Spotify is launching AI audiobook creation tools that use generative AI voices "
                        "from ElevenLabs. The product could shift how authors create synthetic audio and "
                        "how AI-generated media gets distributed."
                    ),
                    link_score=0.95,
                )
            )
        ],
    )

    assert len(prepared) == 1
    assert prepared[0].tier == "lead"
