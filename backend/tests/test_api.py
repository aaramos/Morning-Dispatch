from __future__ import annotations

import re

from fastapi.testclient import TestClient

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian import enrichment
from backend.app.main import create_app
from backend.app.services import digest_runner


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
                text="This article has enough extracted text to stand in for a fetched article body.",
                excerpt="This article has enough extracted text to stand in for a fetched article body.",
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
        assert profiles.json()[0]["name"] == "Adrian"

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

        issue = client.get(f"/api/digests/{digest['id']}/issues/latest")
        assert issue.status_code == 200
        issue_id = issue.json()["id"]

        html = client.get(f"/api/issues/{issue_id}/html")
        assert html.status_code == 200
        assert "Morning Dispatch Issue" in html.text
        assert "A useful newsletter body" in html.text
        assert "May 20, 2026" in html.text
        assert "Model update" in html.text
        assert "Read Online" in html.text
        assert "jwt_token" not in html.text
        assert "media.example.com" not in html.text
        assert "2026-05-20T12:00:00+00:00" not in html.text
        assert "05/20/2026" in html.text
        assert re.search(r"Generated \d{2}/\d{2}/\d{4} ", html.text)
        assert "https://example.com/model-release" in html.text
        assert "Fetched Articles" in html.text
        assert "data-feedback-signal" in html.text
        assert "overflow-x: hidden" in html.text
        assert "overflow-wrap: anywhere" in html.text

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
        assert "Morning Dispatch Issue" in brief.text
        assert "https://example.com/model-release" in brief.text
        assert "05/20/2026" in brief.text
        assert re.search(r"Generated \d{2}/\d{2}/\d{4} ", brief.text)
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
        assert isinstance(status_payload["model_jobs"], list)
        assert status_payload["digests"][0]["name"] == "AI Morning Brief"

        verification = client.post(f"/api/admin/digests/{digest['id']}/verification-run")
        assert verification.status_code == 200
        verification_payload = verification.json()
        assert verification_payload["status"] == "completed"
        assert verification_payload["published"] is False
        assert verification_payload["reviewed_article_count"] == 1
        assert verification_payload["decision_count"] >= 1

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


def test_digest_run_can_publish_reddit_threads(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "false")

    async def fake_fetch_reddit_threads(*_args, **_kwargs):
        return [
            NormalizedPayload(
                source_type="reddit_thread",
                source_name="r/ollama",
                raw_text=(
                    "Local coding agents are getting useful. "
                    "Builders compare small LLM coding agents, MCP tools, and workflow reliability."
                ),
                original_url="https://reddit.com/r/ollama/comments/thread-1/local_agents/",
                published_at="2026-05-22T12:00:00+00:00",
                metadata={
                    "reddit_thread_id": "thread-1",
                    "title": "Local coding agents are getting useful",
                    "thread_quality_score": 0.72,
                    "subreddit": "ollama",
                },
            )
        ]

    monkeypatch.setattr(digest_runner, "fetch_reddit_threads", fake_fetch_reddit_threads)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/digests",
            json={
                "name": "AI Morning Brief",
                "interest": "Local LLM coding agents and AI product workflows",
                "schedule": "daily",
                "sources": [],
            },
        )
        assert created.status_code == 201
        digest = created.json()

        run = client.post(f"/api/digests/{digest['id']}/run")
        assert run.status_code == 202
        assert run.json()["status"] == "completed"
        assert run.json()["fetched_article_count"] == 1

        issue = client.get(f"/api/digests/{digest['id']}/issues/latest")
        html = client.get(f"/api/issues/{issue.json()['id']}/html")
        assert html.status_code == 200
        assert "Local coding agents are getting useful" in html.text
        assert "reddit.com" in html.text
        assert "via r/ollama" in html.text
        assert "05/22/2026" in html.text


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

    monkeypatch.setattr(digest_runner, "fetch_newsletters", fake_fetch_newsletters)
    monkeypatch.setattr(digest_runner, "fetch_articles_for_payloads", fake_fetch_articles)
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
