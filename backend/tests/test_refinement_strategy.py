"""Strategy-quality and auditability tests for the refinement service.

These exercise the deterministic finalize path (no model client required), which
is exactly what runs in fallback mode and whenever the agent under-produces
source queries. They prove the output is source-specific and that a persisted
diagnostics trail explains why the strategy was produced.
"""

from __future__ import annotations

import json
import re
from backend.app.services import refinement


def _profile(**overrides):
    base = {
        "statement": "Track Nvidia and Samsung for HBM memory",
        "scope": "HBM memory competition",
        "keywords": ["HBM", "memory"],
        "subtopics": [],
        "search_queries": [
            "Nvidia HBM roadmap",
            "Samsung HBM3E yield",
            "SK Hynix HBM capacity",
        ],
        "source_queries": {},
        "source_selection": {"web_search": True},
        "foreign_language_plan": [],
    }
    base.update(overrides)
    return base


def test_fallback_does_not_copy_same_phrase_into_every_source():
    profile = _profile(
        source_selection={
            "web_search": True,
            "youtube": True,
            "podcasts": True,
        }
    )
    result = refinement._fill_defaults(profile)
    sq = result["source_queries"]

    assert sq["web_search"] == [
        "Nvidia HBM roadmap",
        "Samsung HBM3E yield",
        "SK Hynix HBM capacity",
    ]
    # Video/audio lanes are differentiated from web and from each other.
    assert sq["youtube"] != sq["web_search"]
    assert sq["podcasts"] != sq["web_search"]
    assert all(q.endswith("explained") for q in sq["youtube"])
    assert any(q.endswith("podcast") for q in sq["podcasts"])
    assert any(q.endswith("interview") for q in sq["podcasts"])
    assert all(q not in {"AI Daily", "Latent Space AI", "The AI Podcast", "Hard Fork", "Practical AI"} for q in sq["podcasts"])


def test_markets_fallback_is_tickers_only_never_descriptive():
    profile = _profile(
        statement="notes on my weekly pottery class schedule",
        scope="pottery class schedule",
        keywords=["pottery"],
        subtopics=[],
        search_queries=["pottery class schedule"],
        source_selection={"web_search": True, "markets": True},
    )
    result = refinement._fill_defaults(profile)
    markets_queries = result["source_queries"].get("markets", [])

    # Never descriptive phrases — tickers or nothing.
    assert "pottery class schedule" not in markets_queries
    for query in markets_queries:
        assert query == query.upper() or "." in query


def test_foreign_media_fallback_is_native_only():
    # Selected with no plan -> no English leakage (stays empty).
    no_plan = _profile(source_selection={"web_search": True, "foreign_media": True})
    no_plan_result = refinement._fill_defaults(no_plan)
    assert no_plan_result["source_queries"].get("foreign_media", []) == []

    # With a plan -> native queries are used, distinct from the English web phrases.
    with_plan = _profile(
        source_selection={"web_search": True, "foreign_media": True},
        foreign_language_plan=[
            {
                "code": "ko",
                "name": "Korean",
                "native_query": "삼성전자 HBM 양산",
                "native_entity_terms": ["SK하이닉스 HBM"],
                "reason": "Korean memory makers report first in Korean media",
            }
        ],
    )
    with_plan_result = refinement._fill_defaults(with_plan)
    foreign = with_plan_result["source_queries"]["foreign_media"]
    assert "삼성전자 HBM 양산" in foreign
    assert all(query not in with_plan_result["source_queries"]["web_search"] for query in foreign)


def test_disabled_sources_get_no_queries():
    profile = _profile(source_selection={"web_search": True, "youtube": False})
    result = refinement._fill_defaults(profile)
    assert "youtube" not in result["source_queries"]


def test_diagnostics_present_and_survive_coercion():
    profile = _profile(source_selection={"web_search": True, "youtube": True})
    result = refinement._fill_defaults(profile)

    diagnostics = result["refinement_diagnostics"]
    assert diagnostics["readiness_reason"] == "defaults_filled"
    assert diagnostics["source_availability"]["web_search"] == {
        "selected": True,
        "query_count": 3,
    }
    assert diagnostics["source_availability"]["podcasts"] == {
        "selected": False,
        "query_count": 0,
    }
    assert diagnostics["final_source_queries"]["youtube"]
    assert diagnostics["final_search_queries"]

    # A second pass through the canonical coercion must not drop the trail.
    again = refinement._coerce_profile(result)
    assert again["refinement_diagnostics"]["readiness_reason"] == "defaults_filled"


def test_strategy_preview_exposes_diagnostics():
    profile = refinement._fill_defaults(_profile())
    preview = refinement._strategy_preview(profile)
    assert preview["diagnostics"]["readiness_reason"] == "defaults_filled"


def test_podcast_semantic_fields_survive_patch_coercion_review_and_preview():
    profile = refinement._coerce_profile(
        _profile(source_selection={"web_search": True, "podcasts": True})
    )
    patched = refinement._merge_agent_profile_patch(
        profile,
        {
            "direct_episode_queries": ["AI agents"],
            "related_episode_queries": ["developer tools"],
            "negative_constraints": ["crypto"],
            "priority_terms": ["OpenAI"],
            "source_queries": {"podcasts": ["AI agents"]},
        },
        user_text="track AI agents in podcasts",
    )

    assert patched["direct_episode_queries"] == ["AI agents"]
    assert patched["related_episode_queries"] == ["developer tools"]
    assert patched["negative_constraints"] == ["crypto"]
    assert patched["priority_terms"] == ["OpenAI"]

    reviewed = refinement._profile_for_strategy_review(
        profile,
        {
            **patched,
            "direct_episode_queries": ["AI agents", "coding agents"],
            "priority_terms": ["OpenAI", "Anthropic"],
        },
        models={},
    )
    assert reviewed["direct_episode_queries"] == ["AI agents", "coding agents"]
    assert reviewed["priority_terms"] == ["OpenAI", "Anthropic"]
    assert refinement._strategy_fingerprint(profile) != refinement._strategy_fingerprint(reviewed)

    preview = refinement._strategy_preview(reviewed)
    podcast_plan = next(entry for entry in preview["per_source"] if entry["key"] == "podcasts")
    assert podcast_plan["direct_episode_queries"] == ["AI agents", "coding agents"]
    assert podcast_plan["related_episode_queries"] == ["developer tools"]
    assert podcast_plan["negative_constraints"] == ["crypto"]
    assert podcast_plan["priority_terms"] == ["OpenAI", "Anthropic"]


def test_apply_agent_update_ignores_model_ready_without_user_confirmation(monkeypatch):
    # Keep the second model pass inert so the test never touches the network.
    monkeypatch.setattr(refinement, "_critique_search_plan", lambda profile: profile)

    profile = refinement._coerce_profile(_profile())
    agent_update = {
        "profile_patch": {"search_queries": ["Nvidia HBM roadmap", "TSMC CoWoS capacity"]},
        "ready_to_build": True,
        "next_question": None,
        "reasoning_summary": "intent is clear enough to build",
    }
    patched, next_question, ready = refinement._apply_agent_update(
        profile=profile,
        messages=[{"role": "user", "content": "track these for me"}],
        agent_update=agent_update,
        just_go_now=False,
        turn_count=5,
    )

    assert ready is False
    assert next_question
    assert "Nvidia HBM roadmap" in patched["search_queries"]
    assert "TSMC CoWoS capacity" in patched["search_queries"]
    assert patched["refinement_diagnostics"] == {}


def test_just_go_now_readiness_reason():
    profile = refinement._coerce_profile(_profile())
    patched, _next, ready = refinement._apply_agent_update(
        profile=profile,
        messages=[{"role": "user", "content": "just go"}],
        agent_update={"profile_patch": {}, "ready_to_build": False, "next_question": None},
        just_go_now=True,
        turn_count=0,
    )
    assert ready is True
    assert patched["refinement_diagnostics"]["readiness_reason"] == "just_go_now"


def test_refinement_prompt_includes_current_date_hint():
    profile = refinement._coerce_profile(_profile())
    payload = json.loads(
        refinement._build_refinement_agent_prompt(
            profile=profile,
            messages=[],
            turn_count=0,
            just_go_now=False,
        )
    )

    current_profile = payload["current_profile"]
    assert re.search(r"20\d{2}-\d{2}-\d{2}", str(current_profile.get("current_date_utc", "")))
    assert "Today is" in payload["current_date_hint"]
    assert "Use this date when judging freshness windows." in payload["current_date_hint"]
    assert payload["current_date_hint"]


def test_queries_from_statement_no_hardcoded_year_suffix():
    queries = refinement._queries_from_statement("optimizing AI inference chips and memory architecture")
    assert "2025" not in " ".join(queries)
    assert len(queries) > 0
