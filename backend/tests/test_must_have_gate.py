from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery import query_refiner
from backend.agents.discovery.registry import SourceRegistry
from backend.agents.discovery.runner import DiscoveryRunner
from backend.agents.discovery.types import Candidate, CostProfile, SourceAdapterContext, TopicProfile
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.services.explore import _enforce_inclusion_limits


def _runtime(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))
    async def no_expansion(*_args, **_kwargs) -> list[str]:
        return []

    async def no_screen(_profile, candidates, **_kwargs):
        return candidates

    monkeypatch.setattr(query_refiner, "expand_search_strategy", no_expansion)
    monkeypatch.setattr(query_refiner, "screen_candidates", no_screen)


class FakeAdapter:
    def __init__(self, name: str, candidates: list[Candidate]):
        self.name = name
        self.cost_profile = CostProfile("fast", 1.0)
        self.good_for = ("broad_discovery",)
        self._candidates = candidates

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        return self._candidates

    async def fetch(self, candidate: Candidate) -> NormalizedPayload:
        return candidate.payload


def _candidate(
    adapter: str,
    *,
    item_id: str,
    text: str,
    title: str | None = None,
    source_name: str | None = None,
    source_type: str | None = None,
) -> Candidate:
    return Candidate(
        adapter=adapter,
        payload=NormalizedPayload(
            id=item_id,
            source_type=source_type or f"{adapter}_item",
            source_name=source_name or title or f"{adapter} source",
            raw_text=text,
            original_url=f"https://example.com/{item_id}",
            metadata={"title": title or item_id},
        ),
        score=0.9,
    )


def _article(
    *,
    item_id: str,
    title: str,
    text: str = "",
    excerpt: str = "",
    source_type: str = "web_search",
    status: str = "fetched",
    tier: str = "main",
    link_score: float = 0.9,
    payload_metadata: dict | None = None,
    metadata: dict | None = None,
) -> ArticleFetchResult:
    payload = NormalizedPayload(
        id=item_id,
        source_type=source_type,
        source_name=title,
        raw_text=text,
        original_url=f"https://example.com/{item_id}",
        metadata={"title": title, **dict(payload_metadata or {})},
    )
    return ArticleFetchResult(
        payload=payload,
        original_url=payload.original_url or "",
        final_url=payload.original_url,
        title=title,
        text=text,
        excerpt=excerpt,
        domain="example.com",
        status=status,
        link_score=link_score,
        tier=tier,
        metadata=dict(metadata or {}),
    )


def test_must_have_gate_requires_each_anchor_and_records_exclusions(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    profile = TopicProfile.from_dict(
        {
            "statement": "Mexico City local food and transit",
            "scope": "CDMX museums and street food",
            "must_have_terms": ["Mexico City", "tacos"],
            "must_have_aliases": {"mexico city": ["CDMX", "Ciudad de México"]},
        }
    )
    keep = _candidate("web_search", item_id="keep", title="CDMX tacos guide", text="CDMX tacos stands and transit notes.")
    missing_tacos = _candidate("web_search", item_id="missing-tacos", title="CDMX museums", text="CDMX museums and transit.")
    missing_city = _candidate("web_search", item_id="missing-city", title="Taco ranking", text="A taco ranking without the required city.")

    result = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("web_search", [keep, missing_tacos, missing_city])])).run(
            profile,
            context=SourceAdapterContext(exploration_id="must-have-1", candidate_limit=10),
        )
    )

    assert [candidate.payload.id for candidate in result.candidates] == ["keep"]
    exclusions = {entry["candidate_id"]: entry for entry in result.exclusions}
    assert exclusions["missing-tacos"]["excluded_by"] == ["must_have"]
    assert "tacos" in exclusions["missing-tacos"]["reason"]
    assert "Mexico City" in exclusions["missing-city"]["reason"]


def test_must_have_gate_matches_accented_aliases_for_foreign_media(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    profile = TopicProfile.from_dict(
        {
            "statement": "Mexico City food coverage",
            "scope": "Local CDMX coverage",
            "must_have_terms": ["Mexico City"],
            "must_have_aliases": {"mexico city": ["Ciudad de México"]},
            "source_selection": {"foreign_media": True},
        }
    )
    foreign = _candidate(
        "foreign_media",
        item_id="foreign",
        source_type="foreign_web",
        title="Ciudad de Mexico food",
        text="Cobertura local de Ciudad de Mexico sobre comida.",
    )

    result = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("foreign_media", [foreign])])).run(
            profile,
            context=SourceAdapterContext(exploration_id="must-have-foreign", candidate_limit=10),
        )
    )

    assert [candidate.payload.id for candidate in result.candidates] == ["foreign"]
    assert result.exclusions == ()


def test_must_have_gate_defers_only_empty_shells(monkeypatch, tmp_path) -> None:
    _runtime(monkeypatch, tmp_path)
    profile = TopicProfile.from_dict(
        {
            "statement": "AI release notes",
            "scope": "Catchpoint and analytics tools",
            "must_have_terms": ["Catchpoint"],
            "requested_sources": [{"adapter": "gmail", "ref": "The Rundown AI"}],
            "source_selection": {"gmail": True, "markets": True, "web_search": True},
        }
    )
    requested = _candidate("gmail", item_id="requested", source_name="The Rundown AI", text="AI newsletter without the anchor.")
    market = _candidate("markets", item_id="market", source_name="MSFT", text="$500.00 +1.2% today")
    empty_shell = Candidate(
        adapter="web_search",
        payload=NormalizedPayload(
            id="empty",
            source_type="web_search_item",
            source_name="",
            raw_text="",
            original_url="https://example.com/empty",
            metadata={},
        ),
        score=0.5,
    )
    off_topic = _candidate("web_search", item_id="drop", title="Google Analytics update", text="GA4 implementation notes.")

    result = asyncio.run(
        DiscoveryRunner(
            SourceRegistry([
                FakeAdapter("gmail", [requested]),
                FakeAdapter("markets", [market]),
                FakeAdapter("web_search", [empty_shell, off_topic]),
            ])
        ).run(
            profile,
            context=SourceAdapterContext(exploration_id="must-have-exemptions", candidate_limit=10),
        )
    )

    assert {candidate.payload.id for candidate in result.candidates} == {"empty"}
    assert any(entry["candidate_id"] == "requested" and entry["excluded_by"] == ["must_have"] for entry in result.exclusions)
    assert any(entry["candidate_id"] == "market" and entry["excluded_by"] == ["must_have"] for entry in result.exclusions)
    assert any(entry["candidate_id"] == "drop" and entry["excluded_by"] == ["must_have"] for entry in result.exclusions)


def test_topic_profile_round_trips_must_have_terms_and_aliases() -> None:
    profile = TopicProfile.from_dict(
        {
            "topic_id": "topic-1",
            "statement": "Track product releases",
            "scope": "Catchpoint release notes",
            "must_have_terms": ["Catchpoint"],
            "must_have_aliases": {"Catchpoint": ["Catchpoint SRE"]},
        }
    )

    payload = profile.to_dict()

    assert payload["must_have_terms"] == ["Catchpoint"]
    assert payload["must_have_aliases"] == {"catchpoint": ["Catchpoint SRE"]}


def test_query_refiner_anchors_model_generated_queries(monkeypatch, tmp_path) -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        async def complete_json(self, **kwargs):
            captured["prompt"] = kwargs["prompt"]
            return {"refined_queries": ["release notes", "GA4 tips", "Catchpoint outage analysis"]}

    _runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        query_refiner.model_routing,
        "client_for_agent",
        lambda *_args, **_kwargs: SimpleNamespace(client=FakeClient()),
    )
    profile = TopicProfile.from_dict(
        {
            "statement": "Tool release notes",
            "scope": "Catchpoint release notes",
            "must_have_terms": ["Catchpoint"],
            "must_have_aliases": {"catchpoint": ["Catchpoint SRE"]},
        }
    )

    refined = asyncio.run(
        query_refiner.refine_queries_for_adapter(
            "web_search",
            profile,
            initial_results=[],
            initial_queries=["Catchpoint release notes"],
            lookback_hours=168,
        )
    )

    prompt = json.loads(captured["prompt"])
    assert prompt["must_have_terms"] == ["Catchpoint"]
    assert refined == ["release notes Catchpoint", "GA4 tips Catchpoint", "Catchpoint outage analysis"]


def test_enforce_must_have_on_queries_preserves_alias_hits() -> None:
    profile = TopicProfile.from_dict(
        {
            "statement": "Mexico City travel",
            "scope": "CDMX travel",
            "must_have_terms": ["Mexico City"],
            "must_have_aliases": {"mexico city": ["CDMX"]},
        }
    )

    assert query_refiner.enforce_must_have_on_queries(profile, ["CDMX tacos", "bike routes"]) == [
        "CDMX tacos",
        "bike routes Mexico City",
    ]


def test_canonicalize_must_have_incident_data():
    from backend.app.services.refinement import _canonicalize_must_have
    terms = ["Mexico City", "CDMX", "Ciudad de México"]
    aliases = {
        "mexico city": ["CDMX"],
        "cdmx": ["Ciudad de México", "DF"],
    }
    canonical_terms, canonical_aliases = _canonicalize_must_have(terms, aliases)
    assert canonical_terms == ["Mexico City"]
    assert "mexico city" in canonical_aliases
    assert set(canonical_aliases["mexico city"]) == {"CDMX", "Ciudad de México", "DF"}


def test_canonicalize_must_have_distinct_anchors():
    from backend.app.services.refinement import _canonicalize_must_have
    terms = ["Tesla", "Battery"]
    aliases = {
        "tesla": ["TSLA", "Elon Musk"],
        "battery": ["cells", "pack"],
    }
    canonical_terms, canonical_aliases = _canonicalize_must_have(terms, aliases)
    assert set(canonical_terms) == {"Tesla", "Battery"}
    assert canonical_aliases["tesla"] == ["TSLA", "Elon Musk"]
    assert canonical_aliases["battery"] == ["cells", "pack"]


def test_canonicalize_must_have_accent_case_folding():
    from backend.app.services.refinement import _canonicalize_must_have
    terms = ["Caffè", "caffe"]
    canonical_terms, canonical_aliases = _canonicalize_must_have(terms, {})
    assert canonical_terms == ["Caffè"]


def test_canonicalize_must_have_symmetric_pollution():
    from backend.app.services.refinement import _canonicalize_must_have
    terms = ["CDMX", "Mexico City"]
    aliases = {"mexico city": ["CDMX"]}
    canonical_terms, canonical_aliases = _canonicalize_must_have(terms, aliases)
    assert canonical_terms == ["CDMX"]
    assert "cdmx" in canonical_aliases
    assert "Mexico City" in canonical_aliases["cdmx"]


def test_merge_agent_profile_patch_synonym_folding():
    from backend.app.services.refinement import _merge_agent_profile_patch
    profile = {
        "must_have_terms": ["Mexico City"],
        "must_have_aliases": {"mexico city": ["CDMX", "Ciudad de México"]}
    }
    patch = {
        "must_have_terms": ["Ciudad de México"],
        "must_have_aliases": {"ciudad de méxico": ["DF"]}
    }
    updated = _merge_agent_profile_patch(profile, patch)
    assert updated["must_have_terms"] == ["Mexico City"]
    assert "mexico city" in updated["must_have_aliases"]
    assert set(updated["must_have_aliases"]["mexico city"]) == {"CDMX", "Ciudad de México", "DF"}


def test_expand_must_have_aliases_returns_canonicalized(monkeypatch):
    from backend.app.services.refinement import expand_must_have_aliases
    class FakeClient:
        async def complete_json(self, **kwargs):
            return {"aliases": {"cdmx": ["Ciudad de México", "Mexico City", "DF"]}}

    monkeypatch.setattr(
        query_refiner.model_routing,
        "client_for_agent",
        lambda *_args, **_kwargs: SimpleNamespace(client=FakeClient()),
    )
    profile = {
        "must_have_terms": ["Mexico City", "CDMX"],
        "must_have_aliases": {}
    }
    terms, aliases = asyncio.run(expand_must_have_aliases(profile))
    assert terms == ["Mexico City"]
    assert set(aliases["mexico city"]) == {"CDMX", "Ciudad de México", "DF"}


def test_expand_must_have_aliases_fail_open(monkeypatch):
    from backend.app.services.refinement import expand_must_have_aliases
    monkeypatch.setattr(
        query_refiner.model_routing,
        "client_for_agent",
        lambda *_args, **_kwargs: SimpleNamespace(client=None),
    )
    profile = {
        "must_have_terms": ["Mexico City", "CDMX"],
        "must_have_aliases": {"mexico city": ["CDMX"]}
    }
    terms, aliases = asyncio.run(expand_must_have_aliases(profile))
    assert terms == ["Mexico City"]
    assert set(aliases["mexico city"]) == {"CDMX"}


def test_runner_verbatim_poisoned_profile_regression(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    profile = TopicProfile.from_dict({
        "statement": "Mexico City testing",
        "scope": "Mexico City news",
        "must_have_terms": ["Mexico City", "CDMX", "Ciudad de México"],
        "must_have_aliases": {
            "mexico city": ["CDMX"],
            "cdmx": ["Ciudad de México"],
        }
    })
    
    keep = _candidate("web_search", item_id="keep", title="CDMX news", text="CDMX local update.")
    
    result = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("web_search", [keep])])).run(
            profile,
            context=SourceAdapterContext(exploration_id="must-have-regression", candidate_limit=10),
        )
    )
    
    assert [candidate.payload.id for candidate in result.candidates] == ["keep"]


def test_funnel_guardrail_diagnostics_note_triggers(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    profile = TopicProfile.from_dict({
        "statement": "Test profile",
        "scope": "Test scope",
        "must_have_terms": ["TargetTerm"],
    })
    
    candidates = []
    for i in range(20):
        candidates.append(_candidate("web_search", item_id=f"miss-{i}", text="No target term here."))
    for i in range(5):
        candidates.append(_candidate("web_search", item_id=f"keep-{i}", text="Here is TargetTerm."))
        
    result = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("web_search", candidates)])).run(
            profile,
            context=SourceAdapterContext(exploration_id="must-have-guardrail-trigger", candidate_limit=50),
        )
    )
    
    assert len(result.notes) == 1
    note = result.notes[0]
    assert note["source_name"] == "Must-Have Gate"
    assert "Warning: Must-have gate rejected 20 of 25" in note["reason"]
    exclusions = [e for e in result.exclusions if e.get("excluded_by") == ["must_have"]]
    assert len(exclusions) == 20
    assert all(exc["missed_terms"] == ["TargetTerm"] for exc in exclusions)


def test_funnel_guardrail_diagnostics_note_silent(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    profile = TopicProfile.from_dict({
        "statement": "Test profile",
        "scope": "Test scope",
        "must_have_terms": ["TargetTerm"],
    })
    
    candidates_a = [_candidate("web_search", item_id=f"miss-{i}", text="No target term.") for i in range(19)]
    result_a = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("web_search", candidates_a)])).run(
            profile,
            context=SourceAdapterContext(exploration_id="must-have-silent-a", candidate_limit=50),
        )
    )
    assert len(result_a.notes) == 0

    candidates_b = []
    for i in range(20):
        candidates_b.append(_candidate("web_search", item_id=f"miss-{i}", text="No target term."))
    for i in range(20):
        candidates_b.append(_candidate("web_search", item_id=f"keep-{i}", text="TargetTerm is here."))
    result_b = asyncio.run(
        DiscoveryRunner(SourceRegistry([FakeAdapter("web_search", candidates_b)])).run(
            profile,
            context=SourceAdapterContext(exploration_id="must-have-silent-b", candidate_limit=50),
        )
    )
    assert len(result_b.notes) == 0


def test_final_must_have_gate_drops_observed_mexico_city_leaks() -> None:
    profile = TopicProfile.from_dict({
        "statement": "Solo trip to Mexico City",
        "scope": "Mexico City, CDMX, Ciudad de México food and walking routes",
        "must_have_terms": ["Mexico City"],
        "must_have_aliases": {"mexico city": ["CDMX", "Ciudad de México"]},
        "source_selection": {"web_search": True},
        "content_limits": {"per_source": {"web_search": 20}},
    })
    results = [
        _article(item_id="egypt", title="Tips for Avoiding Scams in Egypt", text="Cairo tourist scam safety advice."),
        _article(item_id="singapore", title="Travel Dilemma: Singapore vs. Malaysia for a 4-Day Trip", text="Penang street food and Kuala Lumpur logistics."),
        _article(item_id="tokyo", title="I Take Skillcations on My Own", text="A kintsugi workshop in Tokyo changed solo travel."),
        _article(item_id="colombia", title="First Colombia Trip Itinerary Feedback", text="Bogota and Medellin itinerary feedback."),
        _article(item_id="cdmx", title="Walking through Roma and Condesa CDMX", text="Street food route through Roma Norte."),
        _article(item_id="ciudad", title="Street food in Ciudad de Mexico", text="Comida callejera en Ciudad de Mexico."),
    ]

    final = _enforce_inclusion_limits(profile, results)

    included_titles = [result.title for result in final if result.tier != "dropped"]
    assert included_titles == [
        "Walking through Roma and Condesa CDMX",
        "Street food in Ciudad de Mexico",
    ]
    dropped = {result.payload.id: result for result in final if result.tier == "dropped"}
    assert set(dropped) >= {"egypt", "singapore", "tokyo", "colombia"}
    assert all(result.metadata["must_have_rejection_reason"] == "Missing required term(s): Mexico City." for result in dropped.values())
    matches = {
        result.payload.id: result.metadata.get("must_have_matches")
        for result in final
        if result.tier != "dropped"
    }
    assert matches["cdmx"][0]["alias"] == "cdmx"
    assert matches["ciudad"][0]["alias"] == "ciudad de mexico"


def test_must_have_source_floor_does_not_revive_nonmatching_dropped_item() -> None:
    profile = TopicProfile.from_dict({
        "statement": "Solo trip to Mexico City",
        "scope": "Mexico City travel",
        "must_have_terms": ["Mexico City"],
        "source_selection": {"web_search": True},
    })
    off_topic = _article(
        item_id="source-floor",
        title="General solo travel packing advice",
        text="Packing tips for August trips and food tours.",
        tier="dropped",
        link_score=0.99,
    )

    final = _enforce_inclusion_limits(profile, [off_topic])

    assert len(final) == 1
    assert final[0].tier == "dropped"


def test_must_have_drops_low_yield_context_and_fetch_failed_fallback_without_anchor() -> None:
    profile = TopicProfile.from_dict({
        "statement": "Solo trip to Mexico City",
        "scope": "Mexico City travel",
        "must_have_terms": ["Mexico City"],
        "must_have_aliases": {"mexico city": ["CDMX", "Ciudad de México"]},
        "source_selection": {"web_search": True},
    })
    low_yield_context = _article(
        item_id="context",
        title="Solo travel safety checklist",
        text="Useful context for a first solo trip, food, and walking routes.",
        metadata={"source_audit_decision": "include_as_context"},
    )
    fetch_failed = _article(
        item_id="unresolved",
        title="Yahoo travel tips for summer",
        text="",
        excerpt="HTTP 429",
        status="error",
        tier="lower_confidence",
        link_score=0.95,
    )

    final = _enforce_inclusion_limits(profile, [low_yield_context, fetch_failed])

    assert all(result.tier == "dropped" for result in final)
    assert all(result.metadata["must_have_rejected"] is True for result in final)
