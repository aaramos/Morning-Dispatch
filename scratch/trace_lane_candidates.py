import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.discovery import DiscoveryRunner, SourceAdapterContext, TopicProfile, default_source_registry
from backend.agents.discovery.runner import _apply_exclusions, _apply_topic_relevance, _lane_limit, _dedupe_candidates

async def trace_lane_candidates():
    topic_id = "674ce39e-6d93-4211-882f-a4cd5e91578a"
    record = database.get_topic_profile(topic_id)
    profile = TopicProfile.from_dict(record["profile"])
    
    context = SourceAdapterContext(
        exploration_id="trace-exploration",
        candidate_limit=250,
        lookback_hours=168,
    )
    
    registry = default_source_registry()
    
    # 1. Run all adapters
    adapters = registry.selected(profile.source_selection)
    results = await asyncio.gather(
        *[
            DiscoveryRunner(registry)._run_adapter(adapter, profile, context)
            for adapter in adapters
        ],
    )
    all_raw_candidates = [candidate for adapter_candidates, _status in results for candidate in adapter_candidates]
    print(f"Total raw candidates from all adapters: {len(all_raw_candidates)}")
    for adapter in adapters:
        ac = [c for c in all_raw_candidates if c.adapter == adapter.name]
        print(f" - {adapter.name}: {len(ac)}")
        
    # 2. Exclusions and relevance
    candidates, exclusions = _apply_exclusions(profile, all_raw_candidates)
    candidates, relevance_exclusions = _apply_topic_relevance(profile, candidates)
    print(f"After exclusions & topic relevance: {len(candidates)} candidates.")
    for adapter in adapters:
        ac = [c for c in candidates if c.adapter == adapter.name]
        print(f" - {adapter.name}: {len(ac)}")
        
    # 3. Lanes
    selection = profile.source_selection
    source_plan = (
        ("markets", _lane_limit(profile, "markets", default=50, system_max=50)),
        ("youtube", _lane_limit(profile, "youtube", default=25, system_max=25)),
        ("podcasts", _lane_limit(profile, "podcasts", default=25, system_max=25)),
        ("gmail", _lane_limit(profile, "gmail", default=25, system_max=25)),
    )
    
    lane_candidates = []
    lane_adapters = set()
    for source, source_limit in source_plan:
        if selection.get(source) is not True:
            continue
        raw_cands = [candidate for candidate in candidates if candidate.adapter == source]
        lane_adapters.add(source)
        deduped = _dedupe_candidates(
            sorted(raw_cands, key=lambda c: c.score, reverse=True),
            limit=source_limit,
        )
        print(f"Lane {source}: limit={source_limit}, raw={len(raw_cands)}, deduped={len(deduped)}")
        lane_candidates.extend(deduped)
        
    other_candidates = [candidate for candidate in candidates if candidate.adapter not in lane_adapters]
    print(f"Other candidates: {len(other_candidates)}")
    
    non_lane_capacity = max(0, context.candidate_limit - len(lane_candidates))
    deduped_other = _dedupe_candidates(other_candidates, limit=non_lane_capacity)
    print(f"Non-lane capacity: {non_lane_capacity}, deduped other: {len(deduped_other)}")
    
    final_candidates = sorted(list(lane_candidates) + list(deduped_other), key=lambda c: c.score, reverse=True)
    print(f"Final candidates: {len(final_candidates)}")
    final_gmail = [c for c in final_candidates if c.adapter == "gmail"]
    print(f"Final gmail candidates: {len(final_gmail)}")

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(trace_lane_candidates())
