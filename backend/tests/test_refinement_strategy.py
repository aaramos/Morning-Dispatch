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


def test_strategy_review_drops_stale_queries_for_unselected_sources():
    profile = refinement._coerce_profile(
        _profile(
            source_selection={"web_search": True, "markets": True},
            source_queries={
                "web_search": ["Mexico City taco walking route"],
                "markets": ["AAPL"],
            },
            requested_sources=[
                {"adapter": "markets", "ref": "AAPL"},
                {"adapter": "web_search", "ref": "local Spanish-language food blogs"},
            ],
        )
    )

    reviewed = refinement._profile_for_strategy_review(
        profile,
        {
            **profile,
            "source_selection": {"web_search": True, "markets": False},
        },
        models={},
    )

    assert reviewed["source_selection"]["markets"] is False
    assert "markets" not in reviewed["source_queries"]
    assert reviewed["source_queries"] == {"web_search": ["Mexico City taco walking route"]}
    assert reviewed["requested_sources"] == [
        {"adapter": "web_search", "ref": "local Spanish-language food blogs"}
    ]
    prompt = refinement._build_strategy_refinement_prompt(
        profile=reviewed,
        instruction=refinement._pre_build_strategy_review_instruction(reviewed),
        task="Review the current search strategy immediately before the brief build.",
    )
    prompt_body = json.loads(prompt)
    assert "markets" not in prompt_body["current_profile"]["source_queries"]
    assert prompt_body["current_profile"]["selected_sources"] == ["web_search"]


def test_coerce_profile_rewrites_stale_year_queries_for_recent_strategy():
    current_year = refinement.datetime.now(refinement.UTC).year
    profile = refinement._coerce_profile(
        _profile(
            lookback_hours=168,
            recency_weighting="recent",
            scope="Evolution from WWDC 2024 announcements to current workflows",
            subtopics=["WWDC 2024 Apple Intelligence"],
            keywords=["2024 GUI"],
            search_queries=["Apple WWDC 2024 AI announcements summary"],
            source_queries={
                "web_search": ["best AI productivity apps for Mac 2024"],
                "podcasts": ["consumer AI trends 2024"],
            },
            direct_episode_queries=["WWDC 2024 Apple AI"],
            related_episode_queries=["consumer AI trends 2024"],
            source_selection={"web_search": True, "podcasts": True},
        )
    )

    joined = " ".join([
        profile["scope"],
        *profile["subtopics"],
        *profile["keywords"],
        *profile["search_queries"],
        *profile["source_queries"]["web_search"],
        *profile["source_queries"]["podcasts"],
        *profile["direct_episode_queries"],
        *profile["related_episode_queries"],
    ])
    assert "2024" not in joined
    assert str(current_year) in joined


def test_refinement_visible_reply_never_ends_conversation_without_confirmation():
    profile = refinement._coerce_profile(
        _profile(
            lookback_hours=168,
            recency_weighting="recent",
            search_queries=["Apple Intelligence Mac tools"],
        )
    )
    reply = refinement._refinement_reply_with_required_question(
        "You're absolutely right. I have everything I need and I'm ready to build this now.",
        profile=profile,
        messages=[{"role": "user", "content": "Keep it to the last 7 days."}],
        just_go_now=False,
    )

    lowered = reply.lower()
    assert "ready to build" not in lowered
    assert "everything i need" not in lowered
    assert reply.endswith("?")


def test_refinement_visible_reply_sanitizes_stale_year_for_recent_window():
    current_year = refinement.datetime.now(refinement.UTC).year
    profile = refinement._coerce_profile(
        _profile(
            lookback_hours=168,
            recency_weighting="recent",
            search_queries=["Apple Intelligence Mac tools"],
        )
    )
    reply = refinement._refinement_reply_with_required_question(
        "I will not use WWDC 2024 as the anchor.",
        profile=profile,
        messages=[{"role": "user", "content": "There was no mention of 2024."}],
        just_go_now=False,
    )

    assert "2024" not in reply
    assert str(current_year) in reply
    assert reply.endswith("?")


def test_strategy_cleanup_replaces_stale_query_lanes_on_user_correction():
    profile = refinement._coerce_profile(
        _profile(
            lookback_hours=168,
            recency_weighting="recent",
            search_queries=["Apple WWDC 2024 AI announcements summary"],
            source_queries={
                "web_search": ["Apple WWDC 2024 AI announcements summary"],
                "youtube": ["Apple Intelligence 2024 demos"],
            },
            source_selection={"web_search": True, "youtube": True},
        )
    )

    patched = refinement._merge_agent_profile_patch(
        profile,
        {
            "search_queries": ["Apple Intelligence Mac software June 2026"],
            "source_queries": {
                "web_search": ["Apple Intelligence Mac software June 2026"],
                "youtube": ["Apple Intelligence Mac software demo 2026"],
            },
        },
        user_text="Your last response did not elicit a requirement and there was no mention of 2024 ever. Keep it to last 7 days.",
    )

    assert patched["search_queries"] == ["Apple Intelligence Mac software June 2026"]
    assert patched["source_queries"]["web_search"] == ["Apple Intelligence Mac software June 2026"]
    assert patched["source_queries"]["youtube"] == ["Apple Intelligence Mac software demo 2026"]
    assert "2024" not in " ".join([
        *patched["search_queries"],
        *patched["source_queries"]["web_search"],
        *patched["source_queries"]["youtube"],
    ])


def test_coerce_profile_normalizes_market_queries_to_real_tickers_only():
    profile = refinement._coerce_profile(
        _profile(
            source_selection={"web_search": True, "markets": True},
            source_queries={"markets": ["I", "WWDC", "256", "GB", "RAM", "2024", "Apple"]},
        )
    )

    assert profile["source_queries"]["markets"] == ["AAPL"]


def test_user_can_request_strategy_snapshot_in_chat_reply():
    profile = refinement._coerce_profile(
        _profile(
            scope="Mac Studio local AI workflows",
            lookback_hours=168,
            recency_weighting="recent",
            search_queries=["Apple Intelligence Mac Studio 2026"],
            source_queries={"web_search": ["Apple Intelligence Mac Studio 2026"]},
            source_selection={"web_search": True, "podcasts": True},
            direct_episode_queries=["local AI Mac Studio"],
        )
    )
    reply = refinement._refinement_reply_with_required_question(
        "I updated it.",
        profile=profile,
        messages=[{"role": "user", "content": "I want to see the strategy and add this to it."}],
        just_go_now=False,
    )

    assert "Current strategy:" in reply
    assert "- Scope: Mac Studio local AI workflows" in reply
    assert "- General queries: Apple Intelligence Mac Studio 2026" in reply
    assert "- Web search: Apple Intelligence Mac Studio 2026" in reply
    assert reply.endswith("?")


def test_add_this_to_strategy_updates_executable_queries_deterministically():
    profile = refinement._coerce_profile(
        _profile(
            source_selection={"web_search": True, "youtube": True, "podcasts": True, "markets": True},
            search_queries=["Apple Intelligence Mac Studio 2026"],
            source_queries={
                "web_search": ["Apple Intelligence Mac Studio 2026"],
                "markets": ["Apple"],
            },
        )
    )

    patched = refinement._merge_agent_profile_patch(
        profile,
        {"profile_patch": {}},
        user_text="I want to see the strategy and add this to it: knowledge/reasoning workflows (like massive context window research and long-document analysis).",
    )

    assert "knowledge/reasoning workflows" in patched["search_queries"]
    assert "knowledge/reasoning workflows" in patched["source_queries"]["web_search"]
    assert "knowledge/reasoning workflows" in patched["source_queries"]["youtube"]
    assert "knowledge/reasoning workflows" in patched["source_queries"]["podcasts"]
    assert patched["source_queries"]["markets"] == ["AAPL"]
    assert "knowledge/reasoning workflows" in patched["direct_episode_queries"]


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


def test_strategy_preview_markets_never_invents_tickers_from_prose_acronyms():
    # Acronyms in scope/keywords (WWDC, RAM, GB, UI, UX) must NOT become tickers, but a
    # named company (Apple) and a cashtag should resolve to real symbols.
    profile = refinement._coerce_profile(
        _profile(
            statement="Apple WWDC keynote, RAM and GB specs, UI/UX changes, watching $TSLA",
            scope="Apple developer ecosystem WWDC RAM GB UI UX",
            keywords=["WWDC", "RAM", "GB", "UI", "UX"],
            search_queries=["Apple WWDC announcements"],
            source_selection={"web_search": True, "markets": True},
            source_queries={"markets": []},
        )
    )
    preview = refinement._strategy_preview(profile)
    markets = next(source for source in preview["per_source"] if source["key"] == "markets")
    tickers = markets.get("tickers", [])
    assert "AAPL" in tickers
    assert "TSLA" in tickers
    for junk in ("WWDC", "RAM", "GB", "UI", "UX"):
        assert junk not in tickers


def test_substantive_reply_always_updates_strategy_even_with_empty_model_patch():
    before = refinement._coerce_profile(
        _profile(
            scope="Local AI workflows on Mac Studio",
            search_queries=["Mac Studio local inference"],
            source_selection={"web_search": True, "podcasts": True},
            source_queries={"web_search": ["Mac Studio local inference"]},
        )
    )
    # Model returned nothing useful this turn, but the user gave real topical direction.
    patched = refinement._merge_agent_profile_patch(before, {}, user_text="")
    patched = refinement._ensure_reply_updates_strategy(
        before, patched, "focus on running quantized 70B models with MLX"
    )
    assert refinement._strategy_fingerprint(before) != refinement._strategy_fingerprint(patched)
    blob = " ".join(patched["search_queries"]).lower()
    assert "quantized" in blob or "mlx" in blob
    assert patched["source_queries"]["web_search"] != before["source_queries"]["web_search"]
    assert patched["source_queries"].get("podcasts")


def test_meta_and_negative_replies_do_not_pollute_strategy():
    before = refinement._coerce_profile(
        _profile(
            scope="Local AI workflows on Mac Studio",
            search_queries=["Mac Studio local inference"],
            source_selection={"web_search": True},
            source_queries={"web_search": ["Mac Studio local inference"]},
        )
    )
    for meta in ("show me the strategy", "what is the strategy", "no", "nothing else"):
        patched = refinement._ensure_reply_updates_strategy(before, dict(before), meta)
        assert refinement._strategy_fingerprint(before) == refinement._strategy_fingerprint(patched), meta


def test_strip_refinement_closing_language_preserves_paragraphs():
    text = (
        "I refined the scope to focus on quantized local models.\n\n"
        "Should we prioritize MLX tooling or llama.cpp builds?"
    )
    cleaned = refinement._strip_refinement_closing_language(text)
    assert "\n\n" in cleaned
    assert "quantized local models" in cleaned
    assert cleaned.strip().endswith("?")


def test_parse_chat_payload_surfaces_refinement_intent_and_nullable_recency():
    text = json.dumps(
        {
            "reply": "I'll keep refining this with you.",
            "intent": "confirm_changes",
            "profile_patch": {
                "lookback_hours": None,
                "foreign_regions": ["east_asia"],
                "source_queries": {"web_search": ["AI infrastructure supply chain"]},
            },
        }
    )

    patch, ready, intent = refinement._parse_chat_payload(text)

    assert ready is False
    assert intent == "confirm_changes"
    assert patch["lookback_hours"] is None
    assert patch["foreign_regions"] == ["east_asia"]


def test_parse_chat_payload_build_intent_marks_ready():
    patch, ready, intent = refinement._parse_chat_payload(
        json.dumps(
            {
                "reply": "I'll build it now.",
                "intent": "build",
                "ready": False,
                "profile_patch": {"scope": "AI infrastructure"},
            }
        )
    )

    assert patch["scope"] == "AI infrastructure"
    assert ready is True
    assert intent == "build"
