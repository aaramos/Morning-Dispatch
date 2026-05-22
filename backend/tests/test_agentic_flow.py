from __future__ import annotations

import asyncio

from backend.agents.critic import apply_critic_repairs
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.editorial_decisions import apply_editorial_decisions
from backend.agents.librarian.articles import ArticleFetchResult


class FakeModelClient:
    def __init__(self, payload):
        self.payload = payload
        self.config = type("Config", (), {"model": "fake-agent-model"})()

    async def complete_json(self, **_kwargs):
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
