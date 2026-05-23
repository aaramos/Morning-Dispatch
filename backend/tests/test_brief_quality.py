from __future__ import annotations

from backend.agents.brief_quality import apply_brief_quality_checks, clean_display_text
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult


def article_result(
    *,
    title: str = "AI agent workflow benchmark",
    url: str = "https://example.com/articles/agent-workflow?utm_source=newsletter",
    summary: str = "A practical look at AI agent workflows for product and engineering teams.",
    published_at: str | None = "2026-05-22T12:00:00+00:00",
    fetched_at: str = "2026-05-22T13:00:00+00:00",
    status: str = "fetched",
    domain: str = "example.com",
) -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url=url,
        published_at=published_at,
        fetched_at=fetched_at,
        metadata={"link_text": title},
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=url,
        final_url=url,
        canonical_url=url,
        title=title,
        text=summary,
        excerpt=summary,
        editor_summary=summary,
        domain=domain,
        status=status,
        link_score=0.9,
    )


def test_brief_quality_drops_duplicate_articles() -> None:
    first = article_result()
    duplicate = article_result(title="AI agent workflow benchmark", url="https://example.com/articles/agent-workflow")

    cleaned, decisions = apply_brief_quality_checks([first, duplicate])

    assert len(cleaned) == 1
    assert decisions[0].decision == "duplicate"
    assert decisions[0].action == "drop_article"


def test_brief_quality_repairs_noisy_text_and_missing_date() -> None:
    result = article_result(
        title="<b>OpenAI update</b>",
        summary='**[Read more](https://example.com/read)** View image: (https://img.example.com/a.png) <i>Big shift</i>',
        published_at=None,
    )

    cleaned, decisions = apply_brief_quality_checks([result])

    assert cleaned[0].title == "OpenAI update"
    assert "https://" not in cleaned[0].editor_summary
    assert "View image" not in cleaned[0].editor_summary
    assert cleaned[0].payload.published_at == "2026-05-22T13:00:00+00:00"
    assert any(decision.decision == "missing_date" for decision in decisions)


def test_brief_quality_drops_broken_fetched_link() -> None:
    broken = article_result(url="mailto:tips@example.com")

    cleaned, decisions = apply_brief_quality_checks([broken])

    assert cleaned == []
    assert decisions[0].decision == "broken_link"


def test_brief_quality_drops_low_value_blocked_newsletter_sections() -> None:
    blocked = article_result(
        title="Trending AI Tools",
        url="https://link.mail.beehiiv.com/ss/c/u001.example",
        status="blocked",
        domain="link.mail.beehiiv.com",
    )

    cleaned, decisions = apply_brief_quality_checks([blocked])

    assert cleaned == []
    assert decisions[0].decision == "low_value_fallback"


def test_clean_display_text_removes_html_and_raw_links() -> None:
    cleaned = clean_display_text('Hello <b>world</b> [more](https://example.com/path) https://tracking.example.com')

    assert cleaned == "Hello world more"
