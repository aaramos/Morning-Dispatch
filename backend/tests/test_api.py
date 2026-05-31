from __future__ import annotations

import re

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from backend.agents.agentic import AgentDecision
from backend.agents.digestor import podcast
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian import enrichment
from backend.app.db import database
from backend.app.main import create_app
from backend.app.api import routes
from backend.app.services import email_delivery
from backend.app.services import digest_runner
from backend.app.services import verification
from backend.app.services.brief_title import tight_brief_title
from backend.db.queries import get_watermark


def test_health_and_digest_lifecycle(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")

    async def fake_fetch_newsletters(*_args, **_kwargs):
        link_payload = NormalizedPayload(
            source_type="gmail_link",
            source_name="example@example.com",
            original_url="https://newsletter.example.com/redirect",
            published_at="2026-05-20T12:00:00+00:00",
            metadata={
                "gmail_message_id": "msg-1",
                "sender_email": "example@example.com",
                "parent_subject": "Local model releases",
                "link_text": "Model release article",
            },
        )
        return [
            NormalizedPayload(
                source_type="gmail",
                source_name="example@example.com",
                raw_text=(
                    "A useful newsletter body about local model releases. "
                    "View image: (https://media.example.com/image.png) Caption: <b>Model update</b> "
                    "**[Read Online](https://newsletter.example.com/read?jwt_token=secret)**"
                ),
                published_at="2026-05-20T12:00:00+00:00",
                metadata={
                    "gmail_message_id": "msg-1",
                    "sender_email": "example@example.com",
                    "subject": "Local model releases",
                },
            ),
            link_payload,
        ]

    async def fake_fetch_articles(payloads, **_kwargs):
        link_payload = next(payload for payload in payloads if payload.source_type == "gmail_link")
        return [
            ArticleFetchResult(
                payload=link_payload,
                original_url="https://newsletter.example.com/redirect",
                final_url="https://example.com/model-release",
                title="Model release article",
                text="This article covers local AI infrastructure and model releases in enough detail.",
                excerpt="This article covers local AI infrastructure and model releases in enough detail.",
                domain="example.com",
                status="fetched",
            )
        ]

    monkeypatch.setattr(digest_runner, "fetch_newsletters", fake_fetch_newsletters)
    monkeypatch.setattr(digest_runner, "fetch_articles_for_payloads", fake_fetch_articles)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        profiles = client.get("/api/profiles")
        assert profiles.status_code == 200
        assert profiles.json()[0]["name"] == "Default"

        created = client.post(
            "/api/digests",
            json={
                "name": "AI Morning Brief",
                "interest": "Local AI infrastructure and model releases",
                "schedule": "daily",
                "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
            },
        )
        assert created.status_code == 201
        digest = created.json()
        assert digest["name"] == "AI Morning Brief"
        assert digest["sources"][0]["type"] == "gmail_newsletter"

        run = client.post(f"/api/digests/{digest['id']}/run")
        assert run.status_code == 202
        assert run.json()["status"] == "completed"

        custom_window_run = client.post(f"/api/digests/{digest['id']}/run?lookback_hours=48")
        assert custom_window_run.status_code == 202
        assert custom_window_run.json()["lookback_days"] == 2

        issue = client.get(f"/api/digests/{digest['id']}/issues/latest")
        assert issue.status_code == 200
        issue_id = issue.json()["id"]

        html = client.get(f"/api/issues/{issue_id}/html")
        assert html.status_code == 200
        assert "Morning Dispatch Issue" not in html.text
        assert "A useful newsletter body" in html.text
        assert "May 20, 2026" in html.text
        assert "Model update" in html.text
        assert "Read Online" not in html.text
        assert "jwt_token" not in html.text
        assert "media.example.com" not in html.text
        assert "2026-05-20T12:00:00+00:00" not in html.text
        assert "05/20/2026" in html.text
        assert re.search(r"Generated \d{2}/\d{2}/\d{4} ", html.text)
        assert "issue-footer" not in html.text
        assert "Source scope: last 2 days" in html.text
        assert "Last 2 days" not in html.text
        assert "https://example.com/model-release" in html.text
        assert "Top stories" in html.text
        assert "Fetched Articles" not in html.text
        assert "Digest Stats" not in html.text
        assert "brief-sidebar" in html.text
        assert "About this brief" in html.text
        assert "Provenance" not in html.text
        assert "AI tokens" in html.text
        assert "AI calls" in html.text
        assert "Model tokens" not in html.text
        assert "Unresolved Links" not in html.text
        assert "data-feedback-signal" in html.text
        assert "overflow-x: hidden" in html.text
        assert "overflow-wrap: anywhere" in html.text
        assert "-webkit-line-clamp: 4" in html.text
        assert "font-size: clamp(2.8rem, 8vw, 6.4rem)" not in html.text

        feedback = client.post(
            "/api/feedback",
            json={
                "issue_id": issue_id,
                "url": "https://example.com/model-release",
                "signal": "up",
            },
        )
        assert feedback.status_code == 201
        assert feedback.json()["signal"] == "up"

        brief = client.get("/brief")
        assert brief.status_code == 200
        assert "Morning Dispatch Issue" not in brief.text
        assert "https://example.com/model-release" in brief.text
        assert "05/20/2026" in brief.text
        assert re.search(r"Generated \d{2}/\d{2}/\d{4} ", brief.text)
        assert "issue-footer" not in brief.text
        assert "overflow-x: hidden" in brief.text

        admin_status = client.get("/api/admin/status")
        assert admin_status.status_code == 200
        status_payload = admin_status.json()
        assert status_payload["delivery"]["latest_brief_path"] == "/brief"
        assert status_payload["delivery"]["latest_brief_url"].endswith("/brief")
        assert status_payload["gmail"]["network"] == "loopback-or-tailscale"
        assert status_payload["scheduler"]["enabled"] is False
        assert status_payload["health"]["safe_for_overnight"] is False
        assert status_payload["health"]["problem_count"] >= 1
        assert any(check["name"] == "Gmail" for check in status_payload["health"]["checks"])
        assert status_payload["model_cache"]["record_count"] >= 0
        assert status_payload["inference_metrics"]["record_count"] >= 0
        assert status_payload["fetch_failures"]["total_count"] == 0
        assert status_payload["brief_review"]["counts"]["included"] == 1
        assert status_payload["digest_stats"]["newsletter_count"] == 1
        assert status_payload["digest_stats"]["included_article_count"] == 1
        assert status_payload["digest_stats"]["source_count"] == 1
        assert status_payload["podcast_metrics"]["record_count"] >= 0
        assert status_payload["delivery"]["email"]["enabled"] is False
        assert isinstance(status_payload["model_jobs"], list)
        assert status_payload["digests"][0]["name"] == "AI Morning Brief"

        delivery = client.patch(
            f"/api/admin/digests/{digest['id']}/delivery",
            json={"recipient_email": "adrian@example.com", "enabled": True},
        )
        assert delivery.status_code == 200
        assert delivery.json()["recipient_email"] == "adrian@example.com"
        assert delivery.json()["enabled"] is True

        monkeypatch.setattr(
            email_delivery,
            "send_latest_digest",
            lambda digest_id: {"status": "sent", "digest_id": digest_id, "recipient_email": "adrian@example.com"},
        )
        sent = client.post(f"/api/admin/digests/{digest['id']}/delivery/send-test")
        assert sent.status_code == 200
        assert sent.json()["status"] == "sent"

        verification = client.post(f"/api/admin/digests/{digest['id']}/verification-run")
        assert verification.status_code == 200
        verification_payload = verification.json()
        assert verification_payload["status"] == "completed"
        assert verification_payload["published"] is False
        assert verification_payload["reviewed_article_count"] == 1
        assert verification_payload["decision_count"] >= 1

        podcast_refresh = client.post(f"/api/admin/digests/{digest['id']}/verification-run?force_podcast_refresh=true")
        assert podcast_refresh.status_code == 200
        podcast_refresh_payload = podcast_refresh.json()
        assert podcast_refresh_payload["status"] == "no_podcast_sources"
        assert podcast_refresh_payload["mode"] == "podcast_refresh"

        verified_status = client.get("/api/admin/status").json()
        assert verified_status["agent_decisions"]["record_count"] >= verification_payload["stored_decision_count"]

        decisions = client.get("/api/admin/agent-decisions")
        assert decisions.status_code == 200
        assert decisions.json()["decisions"]

        published = client.post(f"/api/admin/digests/{digest['id']}/verification-run?publish=true")
        assert published.status_code == 200
        published_payload = published.json()
        assert published_payload["status"] == "completed"
        assert published_payload["published"] is True
        assert published_payload["published_run_id"]
        assert published_payload["published_issue_id"]


def test_newsletter_cleanup_removes_boilerplate_without_losing_content():
    cleaned = database._clean_newsletter_text(
        "AlphaSignal Stay updated with today's top AI news, papers, and repos. "
        "Signup | Work With Us | Follow on X | Archive "
        "Hey, Google I/O just handed developers the keys to a fully managed AI stack. "
        "Together with · Today's Author Lior Alexander. Founder of AlphaSignal."
    )

    assert "Hey, Google I/O" in cleaned
    assert "Signup" not in cleaned
    assert "Work With Us" not in cleaned
    assert "Follow on X" not in cleaned
    assert "Today's Author" not in cleaned


def test_newsletter_cleanup_drops_scrambled_email_boilerplate():
    cleaned = database._clean_newsletter_text(
        "Oops! Looks like your email provider is scrambling the email:( "
        "Click here to read it in full online: "
        "We'd hate to see you go, but if you want to unsubscribe, please click here:"
    )

    assert cleaned == ""
    assert database._weak_newsletter_snippet(cleaned) is True


def test_newsletter_cleanup_removes_utility_clusters_and_sponsor_blocks():
    rundown = database._clean_newsletter_text(
        "Read Online | Sign Up | Advertise Follow image link: ( ) Caption: "
        "Good morning, AI enthusiasts. Google’s announcements at I/O gave us a clear picture."
    )
    tldr = database._clean_newsletter_text(
        "Microsoft plans to supply its Maia AI chips to Anthropic. "
        "Sign Up | Advertise | View Online TLDR TOGETHER WITH [Cato Networks] "
        "TLDR AI 2026-05-22 DEFENDING AGAINST ATTACKS. (SPONSOR) Join us."
    )

    assert rundown.startswith("Good morning, AI enthusiasts.")
    assert "Read Online" not in rundown
    assert "Follow image link" not in rundown
    assert tldr == "Microsoft plans to supply its Maia AI chips to Anthropic."
    assert "SPONSOR" not in tldr


def test_issue_renderer_includes_all_ranked_articles_and_newsletters():
    newsletters = [
        NormalizedPayload(
            source_type="gmail",
            source_name=f"newsletter-{index}@example.com",
            raw_text=f"This newsletter contains useful AI product and model workflow context number {index}.",
            published_at="2026-05-20T12:00:00+00:00",
            metadata={"subject": f"Newsletter {index}"},
        )
        for index in range(10)
    ]
    article_results = []
    for index in range(30):
        payload = NormalizedPayload(
            source_type="gmail_link",
            source_name="newsletter@example.com",
            original_url=f"https://example.com/articles/{index}",
            published_at="2026-05-20T12:00:00+00:00",
            metadata={"link_text": f"Article {index}"},
        )
        article_results.append(
            ArticleFetchResult(
                payload=payload,
                original_url=f"https://example.com/articles/{index}",
                final_url=f"https://example.com/articles/{index}",
                title=f"Article {index}",
                text="A useful article about AI agents, models, and product workflows.",
                excerpt="A useful article about AI agents, models, and product workflows.",
                domain="example.com",
                status="fetched",
                tier="lead" if index == 0 else "main" if index < 18 else "lower_confidence",
                section="Models & Labs",
                relevance_score=0.9,
                metadata={
                    "image_url": f"https://images.example.com/article-{index}.jpg",
                    "image_source": "og:image",
                } if index < 3 else {},
            )
        )

    html = database.render_ingested_issue(
        "AI Morning Brief",
        "A complete issue should render every selected item.",
        newsletters,
        article_results,
        lookback_hours=24,
    )
    soup = BeautifulSoup(html, "html.parser")

    assert len(soup.select(".lead-block")) == 1
    assert len(soup.select("article.story-row")) == 17
    assert len(soup.select("article.low-conf-row")) == 12
    assert len(soup.select(".img-strip img")) == 3
    assert len(soup.select(".img-strip a.strip-link[href] img")) == 3
    assert soup.select_one(".brief-header .snapshot") is None
    assert len(soup.select("article.newsletter")) == 10
    assert "additional fetched article" not in html


def test_issue_renderer_can_hide_scanned_newsletters_from_topic_brief():
    newsletters = [
        NormalizedPayload(
            source_type="gmail",
            source_name="ai-news@example.com",
            raw_text="This newsletter is about model releases, AI tools, and coding agents.",
            published_at="2026-05-20T12:00:00+00:00",
            metadata={"subject": "AI newsletter"},
        )
    ]
    article_result = ArticleFetchResult(
        payload=NormalizedPayload(
            source_type="web_search",
            source_name="Web",
            original_url="https://travel.example.com/mexico-city",
            raw_text="Mexico City travel guide.",
        ),
        original_url="https://travel.example.com/mexico-city",
        final_url="https://travel.example.com/mexico-city",
        title="Mexico City Travel Guide",
        text="A useful Mexico City travel guide.",
        excerpt="A useful Mexico City travel guide.",
        domain="travel.example.com",
        status="fetched",
        tier="lead",
    )

    html = database.render_ingested_issue(
        "Mexico City Brief",
        "A topic brief should not show unrelated scanned newsletters.",
        newsletters,
        [article_result],
        lookback_hours=24,
        newsletter_payloads=[],
    )
    soup = BeautifulSoup(html, "html.parser")

    assert soup.select("article.newsletter") == []
    assert soup.select("details.source-notes") == []
    assert "model releases" not in html
    assert "No newsletter bodies were available" not in html
    assert "Mexico City Travel Guide" in html


def test_issue_renderer_surfaces_market_snapshot_sparklines():
    payload = NormalizedPayload(
        source_type="market_snapshot",
        source_name="NVIDIA (NVDA)",
        original_url="https://finance.yahoo.com/quote/NVDA",
        raw_text="Public-market snapshot for NVIDIA.",
        metadata={
            "ticker": "NVDA",
            "company_name": "NVIDIA",
            "current_price": 900.0,
            "currency": "USD",
            "change_1d_pct": 1.2,
            "change_3m_pct": 18.4,
            "price_history": [{"close": 760.0}, {"close": 820.0}, {"close": 900.0}],
        },
    )
    result = ArticleFetchResult(
        payload=payload,
        original_url="https://finance.yahoo.com/quote/NVDA",
        final_url="https://finance.yahoo.com/quote/NVDA",
        canonical_url="https://finance.yahoo.com/quote/NVDA",
        title="NVIDIA (NVDA)",
        text="Public-market snapshot for NVIDIA.",
        excerpt="Public-market snapshot for NVIDIA.",
        domain="finance.yahoo.com",
        status="fetched",
        section="Markets",
        content_type="market",
        metadata=dict(payload.metadata),
    )

    rendered = database.render_ingested_issue(
        "AI Markets Brief",
        "A market-aware brief.",
        [payload],
        [result],
        lookback_hours=72,
    )
    html = BeautifulSoup(rendered, "html.parser")

    market = html.select_one(".market-snapshot")
    assert market is not None
    assert html.select_one(".brief-sidebar .market-snapshot") is not None
    assert html.select_one(".story-column .market-snapshot") is None
    assert "Ticker performance" in market.text
    assert "NVDA" in market.text
    assert "$900.00" in market.text
    assert "+18.4% 3M" in market.text
    assert market.select_one("svg.sparkline path") is not None
    recency = html.find(string="Recency")
    assert recency is not None
    assert recency.find_parent(class_="side-stat").select_one(".side-value").text == "3 days"
    assert html.select_one(".story-list").text.strip() == "No stories were ready for this brief."


def test_saved_issue_display_rewrites_legacy_recency_hours(monkeypatch) -> None:
    monkeypatch.setattr(
        routes.database,
        "get_topic_profile",
        lambda topic_id: {
            "profile": {
                "scope": (
                    "I'm an investor and looking to refine my investment portfolio. "
                    "Help me identify companies poised to benefit greatly from the AI buildout. "
                    "Am especially interested in picks and shovels companies."
                ),
                "keywords": ["ai", "investment"],
                "lookback_hours": 168,
                "exclusions": ["MSN"],
                "source_selection": {
                    "web_search": True,
                    "foreign_media": True,
                    "gmail": True,
                    "markets": True,
                },
            }
        },
    )
    html = """
    <html><head><title>I'm an investor and looking to refine my investment portfolio - Morning Dispatch Issue</title><style>.issue-footer { margin-top: 36px; }</style></head><body>
      <div class="side-stat"><span>Recency</span><strong class="side-value">168h</strong></div>
      <div class="side-stat"><span>Sources</span><strong class="side-value">7</strong></div>
      <section class="img-strip" aria-label="Story images">
        <figure class="strip-frame"><img src="https://example.com/story.jpg" alt="Story" loading="lazy" /></figure>
      </section>
      <p class="snapshot">The issue is led by Story. Top coverage clusters around AI.</p>
      <section class="brief-header"><div class="dateline">Thursday</div><h1>I'm an investor and looking to refine my investment portfolio. Help me identify companies poised to benefit greatly from the AI buildout - Morning Dispatch Issue</h1></section>
      <div class="side-note"><h3>Search strategy</h3><p>Focused on I'm an investor and looking to refine my investment portfolio. Help me identify companies poised to benefit greatly from the AI buildout.; searched Gmail, Reddit, Web Search; with a last 7 days source scope.</p></div>
      <h2 class="lead-title"><a href="https://example.com/story" target="_blank" rel="noreferrer">Story</a></h2>
      <p class="meta">AI warning: 1 model call(s) failed before completion; this token total may be incomplete.</p>
      <footer class="issue-footer">Morning Dispatch · 05/28/2026 5:57 PM PDT</footer>
    </body></html>
    """

    rendered = routes._issue_html_for_display(
        html,
        exploration={
            "topic_id": "topic-1",
        },
    )

    assert "168h" not in rendered
    assert '<strong class="side-value">7 days</strong>' in rendered
    assert "<span>Sources</span>" not in rendered
    assert "<span>Sources searched</span>" in rendered
    assert "The issue is led by Story" not in rendered
    assert 'class="snapshot"' not in rendered
    assert "Morning Dispatch Issue" not in rendered
    assert "Focused on I'm an investor" not in rendered
    assert "Prioritized recent AI infrastructure investment signals" in rendered
    assert "Searched web search, foreign media, approved Gmail newsletters, and market data over the last 7 days, excluding MSN." in rendered
    assert "AI warning:" not in rendered
    assert "this token total may be incomplete" not in rendered
    assert "issue-footer" not in rendered
    assert "05/28/2026 5:57 PM PDT" not in rendered
    assert "<title>AI Picks-and-Shovels Investment Signals</title>" in rendered
    assert "<h1>AI Picks-and-Shovels Investment Signals</h1>" in rendered
    assert '<h2 class="lead-title"><a href="https://example.com/story">Story</a></h2>' in rendered
    soup = BeautifulSoup(rendered, "html.parser")
    image_link = soup.select_one(".img-strip a.strip-link[href='https://example.com/story'] img")
    assert image_link is not None
    assert image_link.get("src") == "https://example.com/story.jpg"


def test_tight_brief_title_summarizes_prompt_language() -> None:
    title = tight_brief_title(
        "I'm an investor and looking to refine my investment portfolio. "
        "Help me identify companies poised to benefit greatly from the AI buildout. "
        "Am especially interested in picks and shovels companies. - Morning Dispatch Issue"
    )

    assert title == "AI Picks-and-Shovels Investment Signals"


def test_issue_renderer_flags_incomplete_ai_token_counts():
    article_result = ArticleFetchResult(
        payload=NormalizedPayload(
            source_type="web_search",
            source_name="Web",
            original_url="https://markets.example.com/memory",
            raw_text="Memory market story.",
        ),
        original_url="https://markets.example.com/memory",
        final_url="https://markets.example.com/memory",
        title="Memory market story",
        text="A useful memory market story.",
        excerpt="A useful memory market story.",
        domain="markets.example.com",
        status="fetched",
        tier="lead",
    )

    html = database.render_ingested_issue(
        "Memory Brief",
        "A topic brief about memory markets.",
        [],
        [article_result],
        lookback_hours=72,
        digest_stats={
            "source_count": 1,
            "newsletter_count": 0,
            "link_count": 1,
            "podcast_episode_count": 0,
            "article_candidate_count": 1,
            "included_article_count": 1,
            "unresolved_count": 0,
            "dropped_count": 0,
            "prompt_tokens": 14808,
            "completion_tokens": 0,
            "total_tokens": 14808,
            "model_call_count": 20,
            "model_success_count": 0,
            "model_failure_count": 20,
            "completion_unavailable_count": 20,
            "model_usage": [
                {
                    "model": "Gemma4-MTP-26B-BF16",
                    "mode": "single",
                    "call_count": 20,
                    "success_count": 0,
                    "failure_count": 20,
                }
            ],
            "search_strategy": {
                "summary": "Focused on memory supplier performance; searched Web Search and Markets with a last 3 days source scope.",
            },
            "processing_seconds": 60,
            "stage_seconds": {"editorial": 0},
        },
    )

    assert "Search strategy" in html
    assert "Focused on memory supplier performance" in html
    assert "AI used" in html
    assert "Gemma4-MTP-26B-BF16 supported article summaries; 0/20 calls completed." in html
    assert "AI tokens" in html
    assert "AI calls" in html
    assert "0/20 ok" in html
    assert "Source scope: last 3 days" in html
    assert "Token detail: 14,808 prompt tokens recorded; completion tokens unavailable." in html
    assert "14,808 prompt + 0 completion" not in html
    assert "AI warning:" not in html
    assert "Completion tokens were unavailable for 20 failed call(s)." not in html
    assert "Editorial + review: not measured" in html
    assert "Editorial: 0 ms" not in html
    assert "Model tokens" not in html


def test_admin_reports_fetch_failures_and_review_counts(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")

    async def fake_fetch_newsletters(*_args, **_kwargs):
        return [
            NormalizedPayload(
                source_type="gmail_link",
                source_name="example@example.com",
                raw_text="Newsletter context says this blocked article matters for local model workflows.",
                original_url="https://example.com/blocked",
                published_at="2026-05-20T12:00:00+00:00",
                metadata={
                    "gmail_message_id": "msg-2",
                    "sender_email": "example@example.com",
                    "parent_subject": "Local model workflows",
                    "link_text": "Blocked local model article",
                },
            )
        ]

    async def fake_fetch_articles(payloads, **_kwargs):
        link_payload = next(payload for payload in payloads if payload.source_type == "gmail_link")
        return [
            ArticleFetchResult(
                payload=link_payload,
                original_url="https://example.com/blocked",
                final_url="https://example.com/blocked",
                canonical_url="https://example.com/blocked",
                title="Blocked local model article",
                text="Newsletter context says this blocked article matters for local model workflows.",
                excerpt="Newsletter context says this blocked article matters for local model workflows.",
                domain="example.com",
                status="blocked",
                error="HTTP 403",
                link_score=0.9,
            )
        ]

    monkeypatch.setattr(digest_runner, "fetch_newsletters", fake_fetch_newsletters)
    monkeypatch.setattr(digest_runner, "fetch_articles_for_payloads", fake_fetch_articles)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        digest = client.post(
            "/api/digests",
            json={
                "name": "AI Morning Brief",
                "interest": "Local model workflows",
                "schedule": "daily",
                "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
            },
        ).json()

        run = client.post(f"/api/digests/{digest['id']}/run")
        assert run.status_code == 202

        status_payload = client.get("/api/admin/status").json()
        assert status_payload["fetch_failures"]["total_count"] == 1
        assert status_payload["fetch_failures"]["groups"][0]["status"] == "blocked"
        assert "HTTP 403" in status_payload["fetch_failures"]["examples"][0]["reason"]
        assert status_payload["brief_review"]["counts"]["unresolved"] == 1

        issue = client.get(f"/api/digests/{digest['id']}/issues/latest")
        html = client.get(f"/api/issues/{issue.json()['id']}/html")
        assert "Unresolved Links" not in html.text
        assert "Blocked local model article" not in html.text
        assert "About this brief" in html.text
        assert "Provenance" not in html.text
        assert "Digest Stats" not in html.text


def test_archived_digests_are_hidden_from_default_lists(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        canonical = client.post(
            "/api/digests",
            json={
                "name": "AI Morning Brief",
                "interest": "Agentic AI",
                "schedule": "daily",
                "sources": [{"type": "gmail_newsletter", "sender": "real@example.com"}],
            },
        ).json()
        duplicate = client.post(
            "/api/digests",
            json={
                "name": "AI Morning Brief",
                "interest": "Agentic AI",
                "schedule": "daily",
                "sources": [{"type": "gmail_newsletter", "sender": "duplicate@example.com"}],
            },
        ).json()

        archived = client.patch(f"/api/digests/{duplicate['id']}", json={"status": "archived"})
        assert archived.status_code == 200

        visible = client.get("/api/digests").json()
        assert [digest["id"] for digest in visible] == [canonical["id"]]

        all_digests = client.get("/api/digests?include_archived=true").json()
        assert {digest["id"] for digest in all_digests} == {canonical["id"], duplicate["id"]}

        admin_status = client.get("/api/admin/status").json()
        assert [digest["id"] for digest in admin_status["digests"]] == [canonical["id"]]


def test_digest_run_can_publish_podcast_episodes(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")

    async def fake_fetch_podcast_episodes(*_args, **_kwargs):
        return [
            NormalizedPayload(
                source_type="podcast_episode",
                source_name="AI Daily Brief",
                raw_text=(
                    "Agentic AI workflows for product teams. Podcast: AI Daily Brief. "
                    "Transcript: OpenAI agents, local LLM infrastructure, and product strategy. "
                    "Teams are moving from prompt experiments into agentic workflows."
                ),
                original_url="https://podcasts.example.com/agentic-ai-workflows",
                published_at="2026-05-22T12:00:00+00:00",
                metadata={
                    "podcast_episode_id": "episode-1",
                    "podcast_title": "AI Daily Brief",
                    "title": "Agentic AI workflows for product teams",
                    "feed_url": "https://feeds.example.com/ai-daily.xml",
                    "episode_url": "https://podcasts.example.com/agentic-ai-workflows",
                    "audio_url": "https://cdn.example.com/audio.mp3",
                    "image_url": "https://podcasts.example.com/artwork.jpg",
                    "episode_quality_score": 0.76,
                    "transcript_source": "transcript",
                },
            )
        ], [
            AgentDecision(
                agent="podcast_scout",
                target="Agentic AI workflows for product teams",
                decision="show_notes_summary",
                action="summarize_show_notes",
                confidence=0.7,
                reason="Podcast Triage found a relevant episode.",
                metadata={},
            )
        ]

    monkeypatch.setattr(digest_runner, "fetch_podcast_episodes", fake_fetch_podcast_episodes)
    monkeypatch.setattr(verification, "fetch_podcast_episodes", fake_fetch_podcast_episodes)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/digests",
            json={
                "name": "AI Morning Brief",
                "interest": "Agentic AI product workflows local LLM infrastructure",
                "schedule": "daily",
                "sources": [{"type": "podcast_search", "query": "AI daily brief"}],
            },
        )
        assert created.status_code == 201
        digest = created.json()

        run = client.post(f"/api/digests/{digest['id']}/run")
        assert run.status_code == 202
        assert run.json()["status"] == "completed"
        assert run.json()["fetched_article_count"] == 1
        watermark = get_watermark(
            str(database.database_path()),
            digest["id"],
            podcast._source_key("https://feeds.example.com/ai-daily.xml"),
        )
        assert watermark == {"last_fetched": "2026-05-22T12:00:00+00:00", "last_id": "episode-1"}

        refresh = client.post(f"/api/admin/digests/{digest['id']}/verification-run?force_podcast_refresh=true")
        assert refresh.status_code == 200
        refresh_payload = refresh.json()
        assert refresh_payload["status"] == "completed"
        assert refresh_payload["mode"] == "podcast_refresh"
        assert refresh_payload["published"] is False
        assert refresh_payload["podcast_episode_count"] == 1
        assert refresh_payload["reviewed_article_count"] == 1

        issue = client.get(f"/api/digests/{digest['id']}/issues/latest")
        html = client.get(f"/api/issues/{issue.json()['id']}/html")
        assert html.status_code == 200
        assert "Agentic AI workflows for product teams" in html.text
        assert "https://podcasts.example.com/agentic-ai-workflows" in html.text
        assert "via AI Daily Brief" in html.text
        assert "05/22/2026" in html.text
        assert "Watch & listen" in html.text
        assert "Listen" in html.text
        assert "media-card" in html.text
        assert "podcast-modal" in html.text
        assert "https://cdn.example.com/audio.mp3" in html.text
        assert "https://podcasts.example.com/artwork.jpg" in html.text
        assert "Transcript" in html.text
        assert "Teams are moving from prompt experiments" in html.text

        admin_status = client.get("/api/admin/status").json()
        assert admin_status["digest_stats"]["podcast_episode_count"] == 1
        assert admin_status["podcasts"]["sources"][0]["type"] == "podcast_search"


def test_digest_run_reuses_cached_model_enrichment(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "true")
    monkeypatch.setenv("MORNING_DISPATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_MODEL", "cache-test-model")
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_MODEL_MAX_ITEMS", "1")

    class CountingModelClient:
        def __init__(self):
            self.calls = 0

        async def complete_json(self, **_kwargs):
            self.calls += 1
            return {
                "title": "Cached Model Release",
                "summary": "The local model refined this article once and future runs should reuse it.",
                "keywords": ["model cache", "local ai"],
                "content_type": "article",
            }

    model_client = CountingModelClient()

    async def fake_fetch_newsletters(*_args, **_kwargs):
        link_payload = NormalizedPayload(
            source_type="gmail_link",
            source_name="example@example.com",
            original_url="https://newsletter.example.com/redirect",
            published_at="2026-05-20T12:00:00+00:00",
            metadata={
                "gmail_message_id": "msg-1",
                "sender_email": "example@example.com",
                "parent_subject": "Local model releases",
                "link_text": "Model release article",
            },
        )
        return [
            NormalizedPayload(
                source_type="gmail",
                source_name="example@example.com",
                raw_text="A useful newsletter body about local model releases.",
                published_at="2026-05-20T12:00:00+00:00",
                metadata={
                    "gmail_message_id": "msg-1",
                    "sender_email": "example@example.com",
                    "subject": "Local model releases",
                },
            ),
            link_payload,
        ]

    async def fake_fetch_articles(payloads, **_kwargs):
        link_payload = next(payload for payload in payloads if payload.source_type == "gmail_link")
        return [
            ArticleFetchResult(
                payload=link_payload,
                original_url="https://newsletter.example.com/redirect",
                final_url="https://example.com/model-release",
                canonical_url="https://example.com/model-release",
                title="Model release article",
                text=(
                    "The local AI model release improves agent workflows, product strategy, and developer tooling. "
                    "It gives teams better local infrastructure controls and makes model evaluation easier."
                ),
                excerpt="The local AI model release improves agent workflows.",
                domain="example.com",
                status="fetched",
                link_score=0.9,
            )
        ]

    async def fake_source_audit(_digest, results, **_kwargs):
        return results, [], {"status": "skipped", "candidate_count": len(results)}

    monkeypatch.setattr(digest_runner, "fetch_newsletters", fake_fetch_newsletters)
    monkeypatch.setattr(digest_runner, "fetch_articles_for_payloads", fake_fetch_articles)
    monkeypatch.setattr(digest_runner, "apply_source_audit", fake_source_audit)
    monkeypatch.setattr(enrichment.ModelClient, "from_settings", staticmethod(lambda _settings: model_client))

    with TestClient(create_app()) as client:
        created = client.post(
            "/api/digests",
            json={
                "name": "AI Morning Brief",
                "interest": "Local AI infrastructure and model releases",
                "schedule": "daily",
                "sources": [{"type": "gmail_newsletter", "sender": "example@example.com"}],
            },
        )
        digest = created.json()

        first_run = client.post(f"/api/digests/{digest['id']}/run")
        second_run = client.post(f"/api/digests/{digest['id']}/run")

        assert first_run.status_code == 202
        assert second_run.status_code == 202
        assert model_client.calls == 1

        issue = client.get(f"/api/digests/{digest['id']}/issues/latest")
        html = client.get(f"/api/issues/{issue.json()['id']}/html")
        assert "Cached Model Release" in html.text
