from __future__ import annotations

import asyncio
from dataclasses import replace

from backend.agents.critic import apply_critic_repairs
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.editorial_decisions import apply_editorial_decisions
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.model import ModelClientError
from backend.agents.source_audit import apply_source_audit
from backend.app.db import database
from backend.app.services import digest_runner


class FakeModelClient:
    def __init__(self, payload):
        self.payload = payload
        self.config = type("Config", (), {"model": "fake-agent-model"})()

    async def complete_json(self, **_kwargs):
        return self.payload


class FakeStreamingModelClient(FakeModelClient):
    def __init__(self, payload, chunks: list[str]):
        super().__init__(payload)
        self.chunks = chunks

    async def complete_json(self, **kwargs):
        callback = kwargs.get("on_token")
        if callable(callback):
            for chunk in self.chunks:
                callback(chunk)
        return self.payload


class FailingModelClient(FakeModelClient):
    async def complete_json(self, **_kwargs):
        raise ModelClientError(
            "model stopped before completing the audit",
            status="model_error",
            prompt_tokens=1234,
            total_ms=432,
        )


class FlakyAuditModelClient(FakeModelClient):
    def __init__(self, payload):
        super().__init__(payload)
        self.prompts: list[str] = []

    async def complete_json(self, **kwargs):
        self.prompts.append(kwargs.get("prompt", ""))
        if len(self.prompts) == 1:
            raise ModelClientError(
                "peer closed connection without sending complete message body",
                status="model_error",
                prompt_tokens=5000,
                total_ms=250,
            )
        return self.payload


def result(title: str, *, score: float = 0.5, tier: str = "main") -> ArticleFetchResult:
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="newsletter@example.com",
        original_url=f"https://example.com/{title.lower().replace(' ', '-')}",
        metadata={"link_text": title},
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url=str(payload.original_url),
        canonical_url=str(payload.original_url),
        title=title,
        text="A detailed article about AI infrastructure and model workflows.",
        excerpt="A detailed article about AI infrastructure and model workflows.",
        domain="example.com",
        status="fetched",
        link_score=0.9,
        relevance_score=score,
        tier=tier,
        section="Noteworthy",
        editor_summary="A detailed article about AI infrastructure and model workflows.",
    )


def test_editorial_agent_can_drop_and_replace_lead():
    articles = [
        result("Weak promo story", score=0.7, tier="lead"),
        result("Strong AI infrastructure story", score=0.6),
    ]
    model = FakeModelClient(
        {
            "decisions": [
                {
                    "index": 0,
                    "decision": "exclude",
                    "confidence": 0.9,
                    "reason": "Promotional and low signal.",
                },
                {
                    "index": 1,
                    "decision": "lead",
                    "section": "AI Infrastructure",
                    "confidence": 0.88,
                    "reason": "Best fit for the digest interest.",
                },
            ]
        }
    )

    updated, decisions = asyncio.run(
        apply_editorial_decisions({"name": "AI Brief", "interest": "AI infrastructure"}, articles, model_client=model)
    )

    assert updated[0].tier == "dropped"
    assert updated[1].tier == "lead"
    assert updated[1].section == "AI Infrastructure"
    assert any(decision.agent == "editorial" and decision.action == "drop" for decision in decisions)


def test_editorial_agent_streams_reasoning_to_callback():
    articles = [
        result("Primary story", score=0.7),
        result("Backup story", score=0.6),
    ]
    reasoning_chunks: list[str] = []
    model = FakeStreamingModelClient(
        {
            "decisions": [
                {
                    "index": 0,
                    "decision": "include",
                    "section": "AI Infrastructure",
                    "confidence": 0.9,
                    "reason": "Primary remains.",
                },
                {
                    "index": 1,
                    "decision": "lead",
                    "section": "Business & Markets",
                    "confidence": 0.8,
                    "reason": "Backup has stronger hook.",
                },
            ],
        },
        chunks=[
            "Reviewing candidates in score order...",
            " Marking article 1 as new lead.",
        ],
    )

    def callback(chunk: str) -> None:
        reasoning_chunks.append(chunk)

    _, decisions = asyncio.run(
        apply_editorial_decisions(
            {"name": "AI Brief", "interest": "AI infrastructure"},
            articles,
            model_client=model,
            reasoning_callback=callback,
        )
    )

    assert "".join(reasoning_chunks) == "Reviewing candidates in score order... Marking article 1 as new lead."
    assert any(decision.agent == "editorial" and decision.action == "batch_article_selection" for decision in decisions)


def test_critic_agent_applies_safe_repairs_only():
    articles = [
        result("Lead story", score=0.7, tier="lead"),
        result("Duplicate promo", score=0.5),
        result("Better lead candidate", score=0.8),
    ]
    model = FakeModelClient(
        {
            "publishable": False,
            "summary": "One promo should be dropped and the lead should be replaced.",
            "findings": [
                {
                    "type": "promotional",
                    "target_index": 1,
                    "severity": "medium",
                    "recommended_action": "drop_article",
                    "reason": "This reads like a promotion.",
                },
                {
                    "type": "weak_lead",
                    "target_index": 2,
                    "severity": "high",
                    "recommended_action": "replace_lead",
                    "reason": "This is stronger than the current lead.",
                },
            ],
        }
    )

    updated, decisions = asyncio.run(
        apply_critic_repairs({"name": "AI Brief", "interest": "AI infrastructure"}, [], articles, model_client=model)
    )

    assert updated[1].tier == "dropped"
    assert updated[2].tier == "lead"
    assert any(decision.agent == "critic" and decision.action == "drop_article" for decision in decisions)
    assert any(decision.agent == "critic" and decision.action == "replace_lead" for decision in decisions)


def test_critic_agent_streams_reasoning_to_callback():
    articles = [
        result("Lead story", score=0.7, tier="lead"),
        result("Duplicate promo", score=0.5),
    ]
    reasoning_chunks: list[str] = []
    model = FakeStreamingModelClient(
        {
            "publishable": False,
            "summary": "One weak promo is dropped; lead is strengthened.",
            "findings": [
                {
                    "type": "promotional",
                    "target_index": 1,
                    "severity": "medium",
                    "recommended_action": "drop_article",
                    "reason": "This reads like a promotion.",
                },
            ],
        },
        chunks=[
            "Critic reviewed both lead candidates.",
            " Removing promotional item at index 1.",
        ],
    )

    def callback(chunk: str) -> None:
        reasoning_chunks.append(chunk)

    _, decisions = asyncio.run(
        apply_critic_repairs(
            {"name": "AI Brief", "interest": "AI infrastructure"},
            [],
            articles,
            model_client=model,
            reasoning_callback=callback,
        )
    )

    assert "".join(reasoning_chunks) == "Critic reviewed both lead candidates. Removing promotional item at index 1."
    assert any(decision.agent == "critic" and decision.action == "drop_article" for decision in decisions)


def test_source_audit_agent_can_exclude_stale_or_low_fit_sources():
    articles = [
        result("Fresh memory market story", score=0.8),
        result("Old NAND roadmap story", score=0.7),
    ]
    model = FakeModelClient(
        {
            "decisions": [
                {
                    "index": 0,
                    "decision": "include",
                    "confidence": 0.9,
                    "constraint_failures": [],
                    "reason": "Fresh and directly relevant.",
                },
                {
                    "index": 1,
                    "decision": "exclude",
                    "confidence": 0.88,
                    "constraint_failures": ["recency"],
                    "reason": "Appears outside the requested three-day window.",
                },
            ],
            "summary": "One stale candidate removed before ranking.",
        }
    )

    updated, decisions, summary = asyncio.run(
        apply_source_audit(
            {
                "statement": "Track Micron, Hynix, Kioxia and SanDisk over the previous 3 days",
                "interest": "memory company performance",
                "exclusions": ["MSN", "Yahoo-like syndicated reposts"],
            },
            articles,
            lookback_hours=72,
            model_client=model,
            inference_run_id="audit-test",
        )
    )

    assert updated[0].tier != "dropped"
    assert updated[1].tier == "dropped"
    assert summary["excluded_count"] == 1
    assert any(decision.agent == "source_audit" and decision.action == "drop_article" for decision in decisions)


def test_source_audit_uses_model_date_to_enforce_window_for_undated_items():
    """When the rule-based extractor found no date, the audit trusts the date the
    model infers and enforces the recency window on it."""
    stale = result("Undated stale roadmap")  # no published_at on payload/metadata
    fresh = result("Undated fresh briefing")
    model = FakeModelClient(
        {
            "decisions": [
                {
                    "index": 0,
                    "decision": "include",
                    "confidence": 0.8,
                    "constraint_failures": [],
                    "resolved_published_date": "2026-03-12",
                    "reason": "Looks relevant.",
                },
                {
                    "index": 1,
                    "decision": "include",
                    "confidence": 0.8,
                    "constraint_failures": [],
                    "resolved_published_date": "2026-05-28",
                    "reason": "Fresh and relevant.",
                },
            ],
            "summary": "Inferred dates for two undated items.",
        }
    )

    # lookback covers anything on/after ~2026-05-22 (run relative to 'now'); the
    # 2026-03-12 item must drop, the recent one must stay with its date applied.
    updated, decisions, summary = asyncio.run(
        apply_source_audit(
            {"statement": "Track AI memory supply over the last 7 days", "interest": "memory"},
            [stale, fresh],
            lookback_hours=24 * 7,
            model_client=model,
            inference_run_id="audit-date-test",
        )
    )

    assert updated[0].tier == "dropped"
    assert "recency" in updated[0].metadata["source_audit"]["constraint_failures"]
    assert updated[1].tier != "dropped"
    assert updated[1].payload.published_at == "2026-05-28"
    assert updated[1].metadata.get("date_source") == "model"


def test_source_audit_does_not_override_existing_date_with_model_guess():
    """A deterministic date already on the payload is authoritative; the model's
    guess must not replace it."""
    article = result("Already dated story")
    article = replace(article, payload=replace(article.payload, published_at="2026-05-27"))
    model = FakeModelClient(
        {
            "decisions": [
                {
                    "index": 0,
                    "decision": "include",
                    "confidence": 0.8,
                    "resolved_published_date": "2020-01-01",
                    "reason": "ok",
                }
            ],
            "summary": "ok",
        }
    )

    updated, _decisions, _summary = asyncio.run(
        apply_source_audit(
            {"statement": "x", "interest": "y"},
            [article],
            lookback_hours=24 * 7,
            model_client=model,
            inference_run_id="audit-date-test-2",
        )
    )

    assert updated[0].tier != "dropped"
    assert updated[0].payload.published_at == "2026-05-27"


def test_source_audit_records_failed_model_attempts(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    database.init_database()

    updated, decisions, summary = asyncio.run(
        apply_source_audit(
            {"statement": "Track Micron news over the previous 3 days"},
            [result("Fresh memory market story", score=0.8)],
            lookback_hours=72,
            model_client=FailingModelClient({}),
            inference_run_id="audit-failure-run",
        )
    )

    token_summary = database.inference_token_summary("audit-failure-run")
    assert updated[0].tier != "dropped"
    assert summary["status"] == "fallback"
    assert summary["issues"] == []
    assert "model stopped before completing" in summary["model_issue"]
    assert any(decision.agent == "source_audit" and decision.action == "pre_rank_audit" for decision in decisions)
    assert token_summary["model_call_count"] == 1
    assert token_summary["model_failure_count"] == 1
    assert token_summary["completion_unavailable_count"] == 1
    assert token_summary["prompt_tokens"] == 1234


def test_source_audit_retries_with_smaller_batch_after_model_drop(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    database.init_database()
    model = FlakyAuditModelClient(
        {
            "decisions": [
                {
                    "index": 0,
                    "decision": "include",
                    "confidence": 0.9,
                    "constraint_failures": [],
                    "reason": "Fresh and directly relevant.",
                }
            ],
            "summary": "Retried with a smaller candidate set.",
        }
    )
    articles = [result(f"Fresh memory market story {index}", score=0.9 - index * 0.05) for index in range(12)]

    updated, decisions, summary = asyncio.run(
        apply_source_audit(
            {"statement": "Track Micron news over the previous 3 days"},
            articles,
            lookback_hours=72,
            model_client=model,
            inference_run_id="audit-retry-run",
        )
    )

    token_summary = database.inference_token_summary("audit-retry-run")
    assert summary["status"] == "completed"
    assert updated[0].tier != "dropped"
    assert any(decision.agent == "source_audit" and decision.action == "pre_rank_audit" for decision in decisions)
    assert len(model.prompts) == 2
    assert len(model.prompts[1]) < len(model.prompts[0])
    assert token_summary["model_failure_count"] == 1


def test_source_audit_fallback_drops_obvious_low_quality_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    database.init_database()
    payload = NormalizedPayload(
        source_type="gmail_link",
        source_name="SanDisk-Kioxia Alliance Through 2034 - Maxthon",
        original_url="https://blog.maxthon.com/2026/02/08/sandisk-kioxia-alliance-through-2034",
    )
    low_quality = ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url=str(payload.original_url),
        canonical_url=str(payload.original_url),
        title="SanDisk-Kioxia Alliance Through 2034 - Maxthon | Privacy Private Browser",
        text="SanDisk Kioxia NAND analysis from a low quality blog.",
        excerpt="SanDisk Kioxia NAND analysis from a low quality blog.",
        domain="blog.maxthon.com",
        status="fetched",
        link_score=0.9,
        relevance_score=0.9,
        tier="main",
        section="Noteworthy",
        editor_summary="SanDisk Kioxia NAND analysis from a low quality blog.",
    )

    updated, decisions, summary = asyncio.run(
        apply_source_audit(
            {
                "statement": "Track Micron, Hynix, Kioxia, and SanDisk; avoid sites like MSN or Yahoo News.",
                "scope": "Memory company performance",
            },
            [low_quality],
            lookback_hours=72,
            model_client=FailingModelClient({}),
            inference_run_id="audit-heuristic-fallback-run",
        )
    )

    assert summary["status"] == "fallback"
    assert summary["excluded_count"] == 1
    assert updated[0].tier == "dropped"
    assert "low-quality blog" in summary["issues"][0]["reason"]
    assert any(decision.agent == "source_audit" and decision.action == "drop_article" for decision in decisions)


def test_source_audit_fallback_uses_company_fit_for_market_snapshots(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(tmp_path))
    database.init_database()
    payload = NormalizedPayload(
        source_type="market_snapshot",
        source_name="Ford Motor Company (F)",
        original_url="https://finance.yahoo.com/quote/F",
    )
    market_snapshot = ArticleFetchResult(
        payload=payload,
        original_url=str(payload.original_url),
        final_url=str(payload.original_url),
        canonical_url=str(payload.original_url),
        title="Ford Motor Company (F)",
        text="Ford stock snapshot.",
        excerpt="Ford stock snapshot.",
        domain="finance.yahoo.com",
        status="fetched",
        link_score=0.8,
        relevance_score=0.8,
        tier="main",
        section="Markets",
        editor_summary="Ford stock snapshot.",
    )

    updated, _decisions, summary = asyncio.run(
        apply_source_audit(
            {
                "statement": "Track Micron, Hynix, Kioxia, and SanDisk; avoid Yahoo-like syndicated news.",
                "scope": "Memory company performance",
            },
            [market_snapshot],
            lookback_hours=72,
            model_client=FailingModelClient({}),
            inference_run_id="audit-market-fallback-run",
        )
    )

    assert summary["status"] == "fallback"
    assert updated[0].tier == "dropped"
    assert "market snapshot does not match" in summary["issues"][0]["reason"]


def test_digest_runner_uses_langgraph_orchestration():
    graph = digest_runner._digest_graph().get_graph()

    assert {
        "ingest_sources",
        "fetch_articles",
        "rank_articles",
        "refine_with_model",
        "review_quality",
        "publish_run",
    }.issubset(set(graph.nodes))
