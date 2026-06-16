import asyncio
import os

os.environ["MORNING_DISPATCH_LIBRARIAN_USE_MODEL"] = "false"

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.editor import (
    build_issue_snapshot,
    core_topic_tokens,
    prepare_issue_articles,
    result_is_core_topic,
    result_is_off_topic,
)
from backend.agents.editorial_decisions import _apply_editorial_payload, _normalize_lead
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.enrichment import enrich_article, enrich_articles


# A concrete, multi-token confirmed topic so the core-vocabulary gate is "specific"
# (>= 5 tokens) and trusts its off-topic verdict enough to drop slipped-through items.
RV_INTEREST = (
    "Recreational vehicle travel, motorhome camping, campground reviews, "
    "RV maintenance, and road trip route planning"
)

_RV_LEAD_TEXT = (
    "A new motorhome from Winnebago targets full-time RV travel with a lighter "
    "chassis and better campground hookups. Owners report easier maintenance on "
    "long road trips and the recreational vehicle market is growing."
)
_RV_SECOND_TEXT = (
    "Campground reservations for recreational vehicle travelers are surging this "
    "summer. A guide to the best RV camping routes and road trip stops, plus "
    "maintenance tips for motorhome owners."
)
_LUNG_CANCER_TEXT = (
    "A dramatic new lung cancer immunotherapy trial reports a stunning survival "
    "breakthrough. Oncologists call the tumor-shrinking results among the most "
    "newsworthy in years for patients facing terminal diagnoses."
)


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


def test_core_topic_helpers_classify_rv_vs_lung_cancer():
    core_tokens = core_topic_tokens(RV_INTEREST)
    assert len(core_tokens) >= 5  # specific enough to trust an off-topic verdict

    rv = enrich_article(result("Winnebago debuts a lighter motorhome", _RV_LEAD_TEXT))
    lung = enrich_article(result("Lung cancer immunotherapy breakthrough", _LUNG_CANCER_TEXT))

    assert result_is_core_topic(rv, core_tokens) is True
    assert result_is_off_topic(rv, core_tokens) is False
    assert result_is_core_topic(lung, core_tokens) is False
    assert result_is_off_topic(lung, core_tokens) is True


def test_prepare_issue_articles_lead_is_on_topic_not_drama():
    # Even though the lung-cancer story is the most "newsworthy"/dramatic and has
    # the strongest link_score, it must never lead an RV brief or outrank RV items.
    prepared = prepare_issue_articles(
        {"interest": RV_INTEREST, "threshold": 0.45},
        asyncio.run(
            enrich_articles(
                [
                    result("Lung cancer immunotherapy breakthrough", _LUNG_CANCER_TEXT, link_score=0.99),
                    result("Winnebago debuts a lighter motorhome", _RV_LEAD_TEXT, link_score=0.80),
                    result("Best RV campground routes this summer", _RV_SECOND_TEXT, link_score=0.78),
                ]
            )
        ),
    )

    titles = [item.title for item in prepared]
    # The off-topic, specific-topic mismatch is dropped as the last line of defense.
    assert "Lung cancer immunotherapy breakthrough" not in titles
    assert prepared, "RV items must survive"
    assert prepared[0].tier == "lead"
    assert "RV" in prepared[0].title or "motorhome" in prepared[0].title.lower()
    # No surviving item is off-topic, so nothing can outrank the RV coverage.
    core_tokens = core_topic_tokens(RV_INTEREST)
    assert all(not result_is_off_topic(item, core_tokens) for item in prepared)


def test_apply_editorial_payload_rejects_off_topic_lead_vote():
    # A degraded/permissive model votes the dramatic off-topic story as the lead.
    core_tokens = core_topic_tokens(RV_INTEREST)
    rv = enrich_article(result("Winnebago debuts a lighter motorhome", _RV_LEAD_TEXT))
    lung = enrich_article(result("Lung cancer immunotherapy breakthrough", _LUNG_CANCER_TEXT))
    results = [lung, rv]

    payload = {
        "decisions": [
            {"index": 0, "decision": "lead", "confidence": 0.99, "reason": "dramatic"},
            {"index": 1, "decision": "include", "confidence": 0.6},
        ]
    }
    updated, _decisions = _apply_editorial_payload(
        results, payload, model_name="test-model", core_tokens=core_tokens
    )

    lung_after = next(r for r in updated if r.title.startswith("Lung cancer"))
    assert lung_after.tier != "lead"  # off-topic lead vote was ignored


def test_normalize_lead_prefers_core_topic_over_first_item():
    # The auto-lead fallback must skip a non-core first item when core coverage exists.
    core_tokens = core_topic_tokens(RV_INTEREST)
    lung = enrich_article(result("Lung cancer immunotherapy breakthrough", _LUNG_CANCER_TEXT))
    rv = enrich_article(result("Winnebago debuts a lighter motorhome", _RV_LEAD_TEXT))

    normalized = _normalize_lead([lung, rv], core_tokens)

    lung_after = next(r for r in normalized if r.title.startswith("Lung cancer"))
    rv_after = next(r for r in normalized if r.title.startswith("Winnebago"))
    assert lung_after.tier != "lead"
    assert rv_after.tier == "lead"


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


def test_prepare_issue_articles_keeps_approved_podcast_latest_even_when_off_topic():
    payload = NormalizedPayload(
        source_type="podcast_episode",
        source_name="Approved Show",
        raw_text="A conversation about urban planning, restaurant economics, and neighborhood zoning.",
        original_url="https://podcasts.example.com/latest",
        published_at="2026-05-20T12:00:00+00:00",
        metadata={
            "title": "City zoning and restaurant economics",
            "podcast_title": "Approved Show",
            "subscribed_show": True,
            "approved_podcast_latest": True,
            "episode_quality_score": 0.8,
        },
    )
    podcast_result = ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url=str(payload.original_url),
        canonical_url=str(payload.original_url),
        title="City zoning and restaurant economics",
        text=payload.raw_text,
        excerpt=payload.raw_text,
        domain="podcasts.example.com",
        status="fetched",
        link_score=0.2,
        content_type="podcast",
        metadata=dict(payload.metadata),
    )

    prepared = prepare_issue_articles(
        {"interest": "AI infrastructure and GPU markets", "threshold": 0.45},
        [podcast_result],
    )

    assert len(prepared) == 1
    assert prepared[0].section == "Podcast Signals"
    assert prepared[0].tier == "lead"
    assert prepared[0].relevance_score >= 0.55


def test_prepare_issue_articles_uses_avoid_terms_as_exclusions():
    prepared = prepare_issue_articles(
        {"interest": "Mexico City food, museums, and walking tours Avoid: fine dining, luxury shopping", "threshold": 0.45},
        [
            enrich_article(
                result(
                    "The 27 Best Restaurants in Mexico City",
                    (
                        "A Mexico City restaurant guide focused on world-class fine dining, Pujol, "
                        "and expensive tasting menus in Polanco."
                    ),
                    link_score=0.98,
                )
            ),
            enrich_article(
                result(
                    "Mexico City Street Food and Museum Walks",
                    (
                        "A Mexico City guide to street food markets, museums, historic neighborhoods, "
                        "and practical walking tours for curious travelers."
                    ),
                    link_score=0.95,
                )
            ),
        ],
    )

    assert len(prepared) == 1
    assert prepared[0].title == "Mexico City Street Food and Museum Walks"


def test_prepare_issue_articles_uses_general_sections_for_non_ai_topics():
    prepared = prepare_issue_articles(
        {"interest": "Mexico City food markets, museums, biking, and walking tours", "threshold": 0.45},
        [
            enrich_article(
                result(
                    "Mexico City Market Food Guide",
                    (
                        "A Mexico City food market guide with tacos, neighborhood walking routes, "
                        "museum stops, and cultural context for travelers."
                    ),
                    link_score=0.95,
                )
            )
        ],
    )

    assert len(prepared) == 1
    assert prepared[0].section == "Food & Drink"


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
