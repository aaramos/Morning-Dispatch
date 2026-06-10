from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery import query_refiner
from backend.agents.discovery.registry import SourceRegistry
from backend.agents.discovery.runner import DiscoveryRunner
from backend.agents.discovery.types import Candidate, CostProfile, SourceAdapterContext, TopicProfile


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


def test_must_have_gate_exempts_requested_sources_markets_and_empty_shells(monkeypatch, tmp_path) -> None:
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

    assert {candidate.payload.id for candidate in result.candidates} >= {"requested", "empty"}
    assert any(entry["candidate_id"] == "drop" and entry["excluded_by"] == ["must_have"] for entry in result.exclusions)
    assert not any(entry["candidate_id"] == "market" and entry["excluded_by"] == ["must_have"] for entry in result.exclusions)


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
