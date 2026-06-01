import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.discovery.runner import _apply_exclusions, _apply_topic_relevance
from backend.agents.discovery import DiscoveryRunner, SourceAdapterContext, TopicProfile, default_source_registry

async def trace_gmail_filtering():
    topic_id = "674ce39e-6d93-4211-882f-a4cd5e91578a"
    record = database.get_topic_profile(topic_id)
    profile = TopicProfile.from_dict(record["profile"])
    
    context = SourceAdapterContext(
        exploration_id="trace-exploration",
        candidate_limit=250,
        lookback_hours=168,
    )
    
    registry = default_source_registry()
    adapters = registry.selected(profile.source_selection)
    
    gmail_adapter = [a for a in adapters if a.name == "gmail"][0]
    candidates = await gmail_adapter.query(profile, context)
    print(f"1. Gmail adapter query returned {len(candidates)} candidates.")
    
    # Trace through exclusion
    ex_candidates, exclusions = _apply_exclusions(profile, candidates)
    print(f"2. After _apply_exclusions: {len(ex_candidates)} candidates remain (dropped {len(exclusions)}).")
    if exclusions:
        print("First few exclusions sample:", exclusions[:3])
        
    # Trace through topic relevance
    rel_candidates, relevance_exclusions = _apply_topic_relevance(profile, ex_candidates)
    print(f"3. After _apply_topic_relevance: {len(rel_candidates)} candidates remain (dropped {len(relevance_exclusions)}).")
    if relevance_exclusions:
        print("First few relevance exclusions sample:", relevance_exclusions[:3])

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(trace_gmail_filtering())
